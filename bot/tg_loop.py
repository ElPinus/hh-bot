"""Telegram channel monitoring loop.

Periodically pulls job channels (data/tg_channels.yaml), turns each new
post into a structured vacancy via the LLM extractor, scores it with the
SAME relevance analyzer the hh.ru autopilot uses, and pushes relevant
hits to the owner's Telegram.

Key difference from the hh.ru autopilot: there is NO auto-apply. On
Telegram you apply by messaging a recruiter / filling a form yourself, so
this loop only notifies (with a copy-paste draft on demand). That also
means it is safe to run in MANUAL_MODE — it never takes an external
action on the user's behalf.

Reuses, rather than duplicates, the autopilot's tuned classification:
  * _is_blacklisted  — title/company topic gate (off-target roles)
  * profile_rescue   — rescue low-scored posts that overlap the configured
                       profile markers
  * SCORE_AUTO_SKIP  — the manual-review floor
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from config import (
    TELEGRAM_ADMIN_ID,
    TG_PULL_INTERVAL,
    TG_LOOKBACK_HOURS,
    TG_LIMIT_PER_CHANNEL,
)
from db.storage import (
    save_vacancy,
    tg_seen_exists,
    tg_seen_mark,
    is_duplicate_already_handled,
    log_action,
)
from parser.tg_client import load_channels, fetch_recent_posts
from ai.tg_extractor import extract_vacancy
from ai.analyzer import analyze_relevance
from ai.variant_router import select_resume_variant
from bot.keyboards import tg_vacancy_keyboard
from bot.autopilot import (
    SCORE_AUTO_SKIP,
    profile_rescue,
    _title_looks_like_ic_role,
    _is_blacklisted,
)

logger = logging.getLogger(__name__)

INITIAL_DELAY = 120  # let the hh autopilot + messages loop start first
POST_PAUSE = 2  # seconds between processed (LLM-touching) posts

# Title-level product / AI signal. Word-boundary matching avoids false hits
# ("email" must not match "ai", Russian "-ии" endings like "технологии" must
# not match "ии"). Used to rescue AI/product-titled roles from the dev-title
# gate below.
_PRODUCT_AI_TITLE_RE = re.compile(
    r"\bproduct\b|продукт|продакт|\bai\b|\bии\b|\bml\b|\bllm\b|genai|gpt|"
    r"machine learning|нейросет|нейронн",
    re.IGNORECASE,
)


def _title_has_product_ai_signal(title: str) -> bool:
    """True if the role title signals product or AI/ИИ work."""
    return bool(title and _PRODUCT_AI_TITLE_RE.search(title))


async def tg_loop(bot: Bot) -> None:
    """Pull channels every TG_PULL_INTERVAL and push relevant posts to TG.

    Reads channels via the credential-free t.me/s/ web mirror — no api
    keys or login needed, so the loop just runs (gated only on
    TG_MONITOR_ENABLED in main.py).
    """
    await asyncio.sleep(INITIAL_DELAY)
    logger.info(
        "TG monitor started: every %d min, lookback %dh, max %d posts/channel",
        TG_PULL_INTERVAL // 60, TG_LOOKBACK_HOURS, TG_LIMIT_PER_CHANNEL,
    )

    while True:
        try:
            await _run_pull_cycle(bot)
        except Exception as e:
            logger.error("TG monitor cycle error: %s", e)

        await asyncio.sleep(TG_PULL_INTERVAL)


async def _run_pull_cycle(bot: Bot) -> None:
    """One pull: fetch recent posts, process the ones we haven't seen."""
    channels = load_channels()
    if not channels:
        logger.warning("TG monitor: no enabled channels in tg_channels.yaml")
        return

    since = datetime.now(timezone.utc) - timedelta(hours=TG_LOOKBACK_HOURS)
    posts = await fetch_recent_posts(
        channels, since=since, limit_per_channel=TG_LIMIT_PER_CHANNEL
    )

    stats = {"new": 0, "vacancies": 0, "notified": 0, "skipped": 0, "errors": 0}
    for post in posts:
        if tg_seen_exists(post.id):
            continue
        try:
            await _process_post(bot, post, stats)
        except Exception as e:
            stats["errors"] += 1
            logger.error("TG monitor: error processing %s: %s", post.id, e)
        await asyncio.sleep(POST_PAUSE)

    if stats["new"]:
        logger.info(
            "TG monitor cycle: %d new post(s), %d vacancy(ies), "
            "%d notified, %d skipped, %d error(s)",
            stats["new"], stats["vacancies"], stats["notified"],
            stats["skipped"], stats["errors"],
        )


async def _process_post(bot: Bot, post, stats: dict) -> None:
    """Extract, score and route one previously-unseen Telegram post."""
    result = await extract_vacancy(post.text)
    if not result.get("ok"):
        # Transient extractor failure — do NOT mark seen so we retry the
        # post next cycle rather than silently losing a possible vacancy.
        stats["errors"] += 1
        return

    # Definitive result — mark seen so we never re-extract this post.
    tg_seen_mark(post.id, post.channel)
    stats["new"] += 1

    if not result.get("is_vacancy"):
        return

    title = result.get("title") or f"Вакансия @{post.channel}"
    company = result.get("company") or f"@{post.channel}"
    vacancy = {
        "id": post.id,
        "source": "telegram",
        "title": title,
        "company": company,
        "salary": result.get("salary", ""),
        "city": result.get("location", ""),
        "url": post.url,
        "description": post.text,
    }

    # Reuse the hh autopilot's hard title/company topic gate.
    blocked, reason = _is_blacklisted(vacancy)
    if blocked:
        logger.info("TG monitor: blacklisted %s — %s", post.id, reason)
        stats["skipped"] += 1
        return

    # Dev-title gate: drop pure IC engineer/developer/analyst titles UNLESS
    # the title signals product or AI/ИИ. With a low score floor the analyzer's
    # dev band would otherwise leak developer roles into the queue. When the
    # configured profile targets product/AI roles, a "Backend Developer" is
    # cut, but "AI Engineer" / "Product Manager" / "Head of AI" pass. Runs
    # before the LLM so dev posts don't even cost an analyze call.
    if _title_looks_like_ic_role(title) and not _title_has_product_ai_signal(title):
        logger.info(
            "TG monitor: skip %s — dev/IC title without product/AI signal: %s",
            post.id, title,
        )
        stats["skipped"] += 1
        return

    # Don't re-ping a role already surfaced (from hh OR a prior TG post).
    if is_duplicate_already_handled(company, title):
        logger.info(
            "TG monitor: duplicate of already-handled %s — %s", company, title
        )
        stats["skipped"] += 1
        return

    # Light pass for TG: cheap flash model, lenient. TG is a discovery feed —
    # the keyword pre-filter + blacklist + dev-title gate already cut the
    # obvious noise; we'd rather surface a borderline role than over-cut it.
    # The precise/expensive pro analysis is reserved for the hh autopilot.
    analysis = await analyze_relevance(vacancy, deep=False)
    vacancy.update(analysis)

    # Route to the best-fitting résumé variant so the copy-paste draft uses the
    # right register (no source feed for a Telegram post -> pure semantic).
    variant_key, _vr = await select_resume_variant(vacancy)
    vacancy["resume_variant"] = variant_key

    save_vacancy(vacancy)
    stats["vacancies"] += 1

    score = vacancy.get("relevance_score", 0)
    overlap_meta = ""
    if score < SCORE_AUTO_SKIP:
        eligible, overlap, matched = profile_rescue(vacancy["description"], title)
        if eligible:
            overlap_meta = f"[overlap={overlap}] cats: {', '.join(matched)}"
        else:
            stats["skipped"] += 1
            return  # below floor and not eligible for profile rescue — drop

    await _notify_vacancy(bot, vacancy, result, overlap_meta)
    stats["notified"] += 1


async def _notify_vacancy(bot: Bot, vacancy: dict, extracted: dict, overlap_meta: str) -> None:
    score = vacancy.get("relevance_score", 0)
    title = vacancy["title"]
    company = vacancy.get("company", "")
    salary = vacancy.get("salary") or "не указана"
    location = vacancy.get("city") or (
        "Remote" if extracted.get("is_remote") else "не указан"
    )
    contact = extracted.get("apply_contact") or "см. в посте"
    multi = " · дайджест (в посте несколько вакансий)" if extracted.get("multi") else ""

    if overlap_meta:
        text = (
            f"🎯 TG OVERLAP-RESCUE{multi}\n"
            f"{title}\n"
            f"{company}\n"
            f"ЗП: {salary}\n"
            f"Формат: {location}\n"
            f"Откликаться: {contact}\n"
            f"{overlap_meta}\n"
            f"(analyzer={score}/100 — низкий, режется по role-type гейту; "
            f"поднято через overlap-маркеры твоего профиля)"
        )
    else:
        emoji = {"high": "[!!!]", "medium": "[!!]", "low": "[!]"}.get(
            vacancy.get("relevance", ""), "[?]"
        )
        text = (
            f"{emoji} TG · {title}{multi}\n"
            f"{company}\n"
            f"ЗП: {salary}\n"
            f"Формат: {location}\n"
            f"Откликаться: {contact}\n"
            f"Релевантность: {score}/100\n"
            f"Причина: {vacancy.get('reason', '')}"
        )

    await bot.send_message(
        TELEGRAM_ADMIN_ID,
        text,
        reply_markup=tg_vacancy_keyboard(vacancy["id"], vacancy["url"]),
    )
    log_action("tg_notify", f"post={vacancy['id']} score={score}")
