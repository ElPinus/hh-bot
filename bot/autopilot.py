"""Autopilot: continuous search + scoring + résumé routing. Auto-apply is opt-in
(AUTO_APPLY_ENABLED); scores below SCORE_AUTO_SKIP are dropped (unless rescued
by a strong profile-marker match); everything else goes to Telegram for manual
review. Posts a periodic summary."""
import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot

from config import (
    TELEGRAM_ADMIN_ID,
    AUTO_APPLY_ENABLED,
    HH_RESUME_SEARCH_URL,
    HH_KEYWORD_FILTERS_ENABLED,
    HH_RATING_FILTER_ENABLED,
    HH_REMOTE_CHECK_ENABLED,
)
from db.storage import (
    save_vacancy,
    save_response,
    mark_skipped,
    get_active_filters,
    get_auto_apply_candidates,
    get_employer_rating_cached,
    save_employer_rating,
    is_duplicate_already_handled,
    vacancy_exists,
    set_vacancy_variant,
    log_action,
)
from parser.hh_client import HHClient
from ai.analyzer import analyze_relevance
from ai.cover_letter import generate_cover_letter
from ai.variant_router import select_resume_variant
from ai.resume_variants import RESUME_VARIANTS
from ai.resume_feeds import enabled_feeds, apply_resume_for, apply_resume_hash_for

logger = logging.getLogger(__name__)

# Shared lock for hh.ru browser access — autopilot and messages_loop both
# need self.page; serialise to avoid clobbering navigation.
HH_LOCK = asyncio.Lock()

# Telegram summary cadence (env-overridable). Empty windows are skipped in
# send_summary (no spam).
SUMMARY_INTERVAL = int(os.getenv("AUTOPILOT_SUMMARY_INTERVAL", str(30 * 60)))
# Pacing knobs are env-overridable so you can dial volume down via .env without
# touching code. If hh.ru starts returning short "you're not a robot" anti-bot
# pages (watch for "short body" warnings in the log), increase the pauses —
# a slower pace avoids the proxy-IP rate limit.
SEARCH_PAUSE = int(os.getenv("AUTOPILOT_SEARCH_PAUSE", str(30 * 60)))  # default: 30 min

# === Scoring thresholds (0-100 relevance from the analyzer) ===
# SCORE_AUTO_APPLY: at or above this the autopilot may auto-apply (only when
#   AUTO_APPLY_ENABLED=true). Keep it high so only very confident hits apply.
# SCORE_AUTO_SKIP: below this the vacancy is auto-skipped as a clear mismatch
#   (unless rescued by a strong profile-marker match — see profile_rescue).
# Everything in between goes to Telegram for manual review. Tune both to your
# analyzer's behaviour and how noisy a review queue you tolerate.
# SCORE_AUTO_SKIP is imported by bot/tg_loop.py too (same floor for the TG monitor).
SCORE_AUTO_APPLY = 90
SCORE_AUTO_SKIP = int(os.getenv("SCORE_AUTO_SKIP", "40"))

# Floor for the profile-rescue path. A vacancy scoring below SCORE_AUTO_SKIP is
# rescued to manual review only if it ALSO scores >= this. Below it we trust the
# analyzer's low score and skip, so a confident mismatch isn't surfaced just
# because the title carries a profile keyword. Env-overridable; raise toward
# SCORE_AUTO_SKIP for stricter surfacing, lower toward 0 for broader rescue.
RESCUE_MIN_SCORE = int(os.getenv("RESCUE_MIN_SCORE", "25"))

MAX_VACANCIES_PER_CYCLE = int(os.getenv("AUTOPILOT_MAX_VACANCIES_PER_CYCLE", "20"))  # SURFACED cards per feed
# Hard cap on EXPENSIVE examinations (description fetch + deep LLM score) per
# feed per cycle. MAX_VACANCIES_PER_CYCLE counts only SURFACED cards (see
# run_search_cycle), so a feed full of sub-floor vacancies could otherwise keep
# fetching+scoring while hunting for its surface quota. This bounds proxy/LLM
# load. Defaults to 4x the surface target; lower it if the anti-bot ("short
# body") warnings reappear.
MAX_ANALYZED_PER_CYCLE = int(
    os.getenv("AUTOPILOT_MAX_ANALYZED_PER_CYCLE", str(MAX_VACANCIES_PER_CYCLE * 4))
)
VACANCY_PAUSE = int(os.getenv("AUTOPILOT_VACANCY_PAUSE", "90"))  # seconds between vacancy analysis
# How many SERP pages to fetch per search. Freshest-first ordering puts new
# vacancies on page 0, so a low value suffices when processing few per cycle
# and keeps proxy gotos down at a fast search cadence. Env-overridable.
SEARCH_PAGES = int(os.getenv("AUTOPILOT_SEARCH_PAGES", "4"))

# === Profile-overlap rescue ===
# Even when the analyzer scores a vacancy below SCORE_AUTO_SKIP, the JD may
# strongly overlap the configured candidate profile (see PROFILE_MARKERS below).
# Rescue such vacancies to manual review with a tag showing which marker
# categories matched.
#
# Each category contributes its weight AT MOST ONCE — multiple hits in one
# category don't compound (defends against "10x Python in one JD" noise).
# profile_rescue() (below) is the single gate, shared by the autopilot, the TG
# monitor (bot/tg_loop.py) and the retroactive script
# (scripts/overlap_rescue_existing.py) so the logic never drifts between them.
PROFILE_OVERLAP_THRESHOLD = 3
# ── TUNE ME ────────────────────────────────────────────────────────────────
# PROFILE_MARKERS below is an ILLUSTRATIVE EXAMPLE for an AI/product-oriented
# job seeker; the keywords are generic placeholders. Replace each category's
# list with the signal words of YOUR OWN profile before relying on the rescue.
# Keep the category KEYS (especially "product/pm" and "web/agency stack"):
# they are referenced by profile_rescue() below and by bot/tg_loop.py +
# scripts/overlap_rescue_existing.py. If you rename or drop a category, update
# those call sites too. Markers work best when SPECIFIC; overly generic tokens
# over-trigger on long JD stack enumerations.
# ───────────────────────────────────────────────────────────────────────────
PROFILE_MARKERS: dict[str, tuple[int, list[str]]] = {
    "ai/llm core": (3, [
        " llm", "large language model", "agentic", " ai agent",
        "multi-agent", "мультиагент",
        "prompt engineering", "промпт-инжиниринг",
        "openai api", "claude api",
        "rag", "fine-tun", " lora",
        "genai", "gen ai", "gen-ai",
        "evaluation pipeline", "human-in-the-loop",
        "vector db", "embedding",
    ]),
    "ai product": (3, [
        "ai product", "ai-product",
        "ai implementation", "ai-implementation",
        "ai transformation", "ai-transformation",
        "ai adoption", "ai-adoption",
        "ai strategy", "ai-strategy",
        "head of ai", "ai-внедрен", "внедрение ai",
    ]),
    "product/pm": (2, [
        # Product manager titles
        "product manager", "product owner", "product lead",
        "head of product", "руководитель продукта",
        # Generic management / lead titles. These ride on title-aware overlap
        # (the title is included in the searched text at the call site) and
        # separate a "manager who codes" from a pure IC developer.
        "team lead", "tech lead", "engineering manager",
        "head of ", "director of",
        "руководитель отдела", "руководитель направления",
        "руководитель группы", "руководитель проект",
        "директор по ", "технический руководитель",
        # PM activities (signal even without a management title)
        "roadmap", "a/b test", "ab-тест",
        "stakeholder", "стейкхолдер",
        " kpi", " okr",
    ]),
    # EXAMPLE backend/frontend stack — swap for the one on your résumé.
    # Keep tokens specific enough that they don't fire on every JD that lists
    # the tech in a long enumeration.
    "tech stack": (2, [
        "fastapi", "django", "asyncio",
        "postgres", "redis", "docker",
        "react", "typescript",
    ]),
    # OPTIONAL second profile — EXAMPLE only. Use this if you have a separate
    # skill set worth surfacing that isn't on your main résumé (a different
    # stack or domain). Otherwise tune the keywords or drop the category (and
    # its references in the call sites named above). Keep markers SPECIFIC and
    # trigger only when COMBINED with another category.
    "web/agency stack": (2, [
        "wordpress", "drupal", "joomla",
        "laravel", "symfony",
        "cms-сайт", "разработ под cms",
        "адаптивн вёрстк", "адаптивн верстк",
    ]),
    "domain": (1, [
        "b2b saas", "on-premise", "on premise",
        "ecommerce", "e-commerce", "marketplace",
        "fintech", "edtech",
    ]),
}


# Title-level engineer/designer markers — these indicate the role is
# an IC engineer/designer position, not a management/PM role. Used by
# `_title_looks_like_ic_role()` to suppress overlap-rescue when title
# is clearly not the configured profile, regardless of how rich the
# JD body is in profile keywords.
_IC_ROLE_TITLE_MARKERS = [
    # English — always preceded by a space (no compound words)
    " developer", " engineer", " designer", " analyst",
    " researcher", " specialist", " programmer", " scientist",
    # Russian — no leading space so hyphenated forms also match
    # ("Backend-разработчик", "ML-инженер", "Frontend-разработчик")
    "разработчик", "программист",
    "инженер", "дизайнер",
    "аналитик", "исследовател", "специалист",
    "архитектор",  # IC architect roles
]

# Title-level management anchors — these "rescue" titles that contain an
# IC marker but ALSO indicate management (e.g. "Engineering Manager",
# "Tech Lead Backend"). If present, overlap-rescue is allowed.
_MANAGEMENT_TITLE_ANCHORS = [
    " manager", "head of", "руководитель", "директор по",
    "team lead", "tech lead", "engineering lead", "engineering manager",
    "chief ", " cto", " cpo", " cdo", " vp ",
    "product manager", "product owner", "product lead",
]


def _title_looks_like_ic_role(title: str) -> bool:
    """True if title is a clear IC engineer/designer/analyst role
    WITHOUT a management anchor. When the configured profile targets
    management / product roles, pure IC roles shouldn't be rescued even when
    the JD body is rich in profile keywords (description-keyword false positives).
    """
    if not title:
        return False
    t = title.lower()
    has_ic_marker = any(m in t for m in _IC_ROLE_TITLE_MARKERS)
    if not has_ic_marker:
        return False
    has_mgmt = any(a in t for a in _MANAGEMENT_TITLE_ANCHORS)
    return not has_mgmt


def profile_overlap_score(
    description: str, title: str = ""
) -> tuple[int, list[str]]:
    """Score how strongly a JD overlaps the configured candidate profile.

    Searches both `title` and `description` — titles often carry the
    cleanest specificity ("Team Lead", "Product Manager"), while
    descriptions can dilute signals in long stack enumerations.

    Returns (total, matched_categories). Each category is all-or-nothing
    (its weight applied once if any of its markers occurs in either
    field), so noisy JDs with the same keyword repeated don't inflate
    the score. Used by profile_rescue() to "rescue" low-LLM-score
    vacancies into manual review — see PROFILE_OVERLAP_THRESHOLD.
    """
    if not description and not title:
        return 0, []
    text = f"{title or ''} {description or ''}".lower()
    total = 0
    matched: list[str] = []
    for cat, (weight, markers) in PROFILE_MARKERS.items():
        if any(m in text for m in markers):
            total += weight
            matched.append(cat)
    return total, matched


def profile_rescue(
    description: str, title: str = ""
) -> tuple[bool, int, list[str]]:
    """Decide whether a below-floor vacancy should be rescued to manual review.

    SINGLE SOURCE OF TRUTH for the rescue gate — used by the hh autopilot,
    the TG monitor (bot/tg_loop.py) and the retroactive rescue script
    (scripts/overlap_rescue_existing.py). Keep the logic here only so the three
    call sites never drift.

    Rescue requires:
      (a) total overlap >= PROFILE_OVERLAP_THRESHOLD, AND
      (b) at least one ROLE-relevant category — "product/pm" (management/lead
          role) OR "web/agency stack" (second-profile roles), so a pure
          tech-stack or domain keyword match alone doesn't rescue, AND
      (c) the title is NOT a clear IC engineer/designer role without a
          management anchor (their JD bodies often mention management/PM words
          for a role the configured profile doesn't take).

    Returns (eligible, overlap, matched_categories). overlap/categories are
    returned even when not eligible so callers can log or tag the card.

    ILLUSTRATIVE gate keyed to the example PROFILE_MARKERS — retune the markers
    and the role-relevant categories below to YOUR profile.
    """
    overlap, cats = profile_overlap_score(description, title)
    role_match = ("product/pm" in cats) or ("web/agency stack" in cats)
    ic_role = _title_looks_like_ic_role(title)
    eligible = overlap >= PROFILE_OVERLAP_THRESHOLD and role_match and not ic_role
    return eligible, overlap, cats


# Minimum employer rating on hh.ru to consider applying. Companies without
# a rating widget (None) are NOT filtered out — small / new companies often
# have no rating yet, and some of them are startups worth applying to.
MIN_COMPANY_RATING = 3.5

# Blacklisted companies — never apply (whole-word match on company name).
# Empty by default. Add employers you never want to apply to, e.g.:
#   COMPANY_BLACKLIST = ["acme corp", "example ltd"]
COMPANY_BLACKLIST: list[str] = []

# Title pre-filter — substring match on the vacancy title (lowercased), runs
# BEFORE the LLM analyzer to save tokens and act as a hard floor against
# analyzer mistakes. EMPTY by default — nothing is pre-filtered, every vacancy
# reaches the analyzer.
#
# TUNE ME: add lowercase title fragments of roles/domains that are DEFINITELY
# NOT for you, so they're dropped without an LLM call. This is YOUR personal
# anti-list — what counts as "off-target" depends entirely on your profile
# (a designer would NOT blacklist "designer"; a salesperson would NOT
# blacklist "sales"). Example for someone who wants to skip a few unrelated
# role types:
#   TITLE_BLACKLIST = ["3d artist", "sales manager", "recruiter", "бухгалтер"]
# Matched against the title only (not the description), so false negatives are
# preferred over false positives.
TITLE_BLACKLIST: list[str] = []

# accumulate stats between summaries
_stats = {
    "found": 0,
    "auto_applied": [],
    "manual_review": 0,
    "auto_skipped": 0,
    "errors": 0,
    "last_summary": datetime.now(),
}


def _reset_stats():
    _stats["found"] = 0
    _stats["auto_applied"] = []
    _stats["manual_review"] = 0
    _stats["auto_skipped"] = 0
    _stats["errors"] = 0
    _stats["last_summary"] = datetime.now()


async def autopilot_loop(bot: Bot, hh_client: HHClient):
    """Main autopilot loop. Searches continuously, posts a periodic summary."""
    await asyncio.sleep(60)  # wait 1 min after startup
    logger.info("Autopilot started")

    # start summary task
    asyncio.create_task(summary_loop(bot))

    while True:
        try:
            async with HH_LOCK:
                logged_in = await hh_client._is_logged_in()
            if not logged_in:
                logger.warning("Autopilot: not logged in, sleeping 10 min")
                await asyncio.sleep(SEARCH_PAUSE)
                continue

            await run_search_cycle(bot, hh_client)

        except Exception as e:
            logger.error("Autopilot error: %s", e)
            _stats["errors"] += 1
            if "429" in str(e) or "rate limit" in str(e).lower():
                logger.warning("Autopilot: rate limited, extra 10 min cooldown")
                await asyncio.sleep(600)

        await asyncio.sleep(SEARCH_PAUSE)


async def summary_loop(bot: Bot):
    """Send a summary every SUMMARY_INTERVAL seconds."""
    while True:
        await asyncio.sleep(SUMMARY_INTERVAL)
        try:
            await send_summary(bot)
        except Exception as e:
            logger.error("Summary send error: %s", e)


async def run_search_cycle(bot: Bot, hh_client: HHClient):
    """Run one search cycle across all configured sources.

    Sources, in priority order:
      1. Per-variant résumé feeds (resume-variants/feeds.yaml), each tagged with
         its variant so the router gets a source hint and the card shows the
         right résumé.
      2. Legacy single HH_RESUME_SEARCH_URL — used only when feeds.yaml has no
         enabled feed (so the bot keeps working with a single résumé).
      3. Keyword filters (the `filters` DB table) — only when
         HH_KEYWORD_FILTERS_ENABLED.
    """
    # Each source is (label, search_thunk, source_variant): source_variant is
    # the résumé variant whose "similar vacancies" feed this is, or None for the
    # legacy single feed / keyword filters. It becomes a soft hint to the router.
    # NB: with several feeds enabled the cycle does one search goto per feed and
    # processes up to MAX_VACANCIES_PER_CYCLE *per feed* — more proxy load than
    # a single feed; tune SEARCH_PAUSE / MAX if the anti-bot warnings reappear.
    sources: list[tuple[str, object, str | None]] = []
    feeds = enabled_feeds()
    if feeds:
        for vkey, url in feeds:
            label = f"резюме #{vkey} {RESUME_VARIANTS[vkey]['short']}"
            sources.append(
                (label,
                 lambda url=url: hh_client.search_by_url(url, pages=SEARCH_PAGES),
                 vkey)
            )
    elif HH_RESUME_SEARCH_URL:
        sources.append(
            ("resume-поиск",
             lambda: hh_client.search_by_url(HH_RESUME_SEARCH_URL, pages=SEARCH_PAGES),
             None)
        )
    if HH_KEYWORD_FILTERS_ENABLED:
        for f in get_active_filters():
            sources.append(
                (f["name"],
                 lambda f=f: hh_client.search_vacancies(f, pages=SEARCH_PAGES),
                 None)
            )
    if not sources:
        logger.warning(
            "Autopilot: no search sources — set HH_RESUME_SEARCH_URL or enable "
            "keyword filters (HH_KEYWORD_FILTERS_ENABLED)"
        )
        return

    for source_label, do_search, source_variant in sources:
        try:
            async with HH_LOCK:
                vacancies = await do_search()
            processed = 0  # SURFACED this cycle (cards sent / auto-applied)
            analyzed = 0   # EXPENSIVE examinations (desc fetch + LLM) this cycle
            for v in vacancies:
                if processed >= MAX_VACANCIES_PER_CYCLE:
                    logger.info("Autopilot: reached %d surfaced limit for this cycle", MAX_VACANCIES_PER_CYCLE)
                    break
                if analyzed >= MAX_ANALYZED_PER_CYCLE:
                    logger.info(
                        "Autopilot: reached %d analysis cap (%d surfaced) for this cycle",
                        MAX_ANALYZED_PER_CYCLE, processed,
                    )
                    break
                # Already examined in a previous cycle? Skip instantly — the
                # résumé search re-returns the same freshest vacancies every
                # few minutes; without this we'd re-open (proxy goto) and
                # re-score (LLM) each one every cycle, flooding the LLM and the
                # anti-bot. New ones (not yet in the table) fall through.
                if vacancy_exists(v["id"]):
                    continue
                # Check company blacklist
                company_lower = v.get("company", "").lower()
                if any(bl in company_lower for bl in COMPANY_BLACKLIST):
                    logger.info("Autopilot: skipping %s (blacklisted company: %s)", v["id"], v.get("company"))
                    v["relevance_score"] = 0
                    v["relevance"] = "low"
                    v["reason"] = f"Компания в черном списке: {v.get('company')}"
                    v["description"] = ""
                    save_vacancy(v)
                    mark_skipped(v["id"])
                    _stats["auto_skipped"] += 1
                    continue

                # Check title topic blacklist (off-target title fragments, if configured)
                title_lower = v.get("title", "").lower()
                blacklisted_topic = next(
                    (bl for bl in TITLE_BLACKLIST if bl in title_lower), None
                )
                if blacklisted_topic:
                    logger.info("Autopilot: skipping %s (title topic: %s) — %s",
                                v["id"], blacklisted_topic, v.get("title"))
                    v["relevance_score"] = 0
                    v["relevance"] = "low"
                    v["reason"] = f"Топик вне профиля: {blacklisted_topic}"
                    v["description"] = ""
                    save_vacancy(v)
                    mark_skipped(v["id"])
                    _stats["auto_skipped"] += 1
                    continue

                # Cross-city / cross-format duplicate check: same employer
                # + same normalized title already handled before.
                if is_duplicate_already_handled(v.get("company", ""), v.get("title", "")):
                    logger.info(
                        "Autopilot: skipping %s (duplicate of already-handled "
                        "vacancy from %s) — %s",
                        v["id"], v.get("company"), v.get("title"),
                    )
                    v["relevance_score"] = 0
                    v["relevance"] = "low"
                    v["reason"] = "Дубликат вакансии того же работодателя (уже обработана)"
                    v["description"] = ""
                    save_vacancy(v)
                    mark_skipped(v["id"])
                    _stats["auto_skipped"] += 1
                    continue

                # Commit to the expensive path (proxy goto + deep LLM score):
                # count it against the per-feed analysis cap.
                analyzed += 1

                async with HH_LOCK:
                    desc = await hh_client.get_vacancy_description(v["url"])
                    v["description"] = desc
                    # check remote + rating use the just-loaded page, must be under same lock
                    is_remote = await hh_client.check_remote_available()
                    rating = await hh_client.get_company_rating()
                    rating_source = "vacancy" if rating is not None else None
                    # Fallback: try the employer page (with 30-day cache).
                    if rating is None:
                        employer_id = await hh_client.get_employer_id_from_vacancy_page()
                        if employer_id:
                            cached_found, cached_rating = get_employer_rating_cached(employer_id)
                            if cached_found:
                                rating = cached_rating
                                rating_source = "cache"
                            else:
                                rating = await hh_client.fetch_employer_rating(employer_id)
                                save_employer_rating(employer_id, rating)
                                rating_source = "employer-page"
                v["company_rating"] = rating or 0
                logger.info(
                    "Vacancy %s | %s | remote=%s | rating=%s (%s)",
                    v["id"], v.get("company", "?")[:40], is_remote,
                    f"{rating:.1f}" if rating is not None else "n/a",
                    rating_source or "none",
                )

                if HH_REMOTE_CHECK_ENABLED and not is_remote:
                    logger.info("Autopilot: skipping %s (no remote), %s", v["id"], v["title"])
                    v["relevance_score"] = 0
                    v["relevance"] = "low"
                    v["reason"] = "Нет удалённой работы"
                    save_vacancy(v)
                    mark_skipped(v["id"])
                    _stats["auto_skipped"] += 1
                    await asyncio.sleep(VACANCY_PAUSE)
                    continue

                if HH_RATING_FILTER_ENABLED and rating is not None and rating > 0 and rating < MIN_COMPANY_RATING:
                    logger.info(
                        "Autopilot: skipping %s (low rating %.1f < %.1f), %s",
                        v["id"], rating, MIN_COMPANY_RATING, v["title"],
                    )
                    v["relevance_score"] = 0
                    v["relevance"] = "low"
                    v["reason"] = f"Низкий рейтинг компании: {rating:.1f}/5 (порог {MIN_COMPANY_RATING})"
                    save_vacancy(v)
                    mark_skipped(v["id"])
                    _stats["auto_skipped"] += 1
                    await asyncio.sleep(VACANCY_PAUSE)
                    continue

                # Precise pro analysis (deep=True): drives the auto-apply
                # decision, so it runs the full scoring against the candidate
                # summary. Affordable here because the per-feed caps bound how
                # many vacancies reach this point each cycle.
                analysis = await analyze_relevance(v, deep=True)
                v.update(analysis)

                if save_vacancy(v):
                    _stats["found"] += 1
                    score = v.get("relevance_score", 0)

                    # Profile-marker awareness: does the JD overlap the configured
                    # profile markers? profile_rescue is the single source of truth
                    # (see its docstring). Used three ways: matched_cats on the card,
                    # strong_profile drives the "🎯 ПО ПРОФИЛЮ" header, and
                    # strong_profile rescues a below-floor vacancy the analyzer's
                    # generic gate wrongly cut.
                    strong_profile, overlap, matched_cats = profile_rescue(
                        v.get("description", ""), v.get("title", ""),
                    )

                    rescued = False
                    if score < SCORE_AUTO_SKIP:
                        # Below the auto-skip floor — drop it UNLESS it strongly
                        # matches the profile markers AND isn't a confident
                        # mismatch (score >= RESCUE_MIN_SCORE).
                        if strong_profile and score >= RESCUE_MIN_SCORE:
                            rescued = True
                            logger.info(
                                "Autopilot: rescuing %s to manual review "
                                "(score=%d, overlap=%d cats: %s) — %s",
                                v["id"], score, overlap,
                                ", ".join(matched_cats), v["title"],
                            )
                        else:
                            if strong_profile:
                                logger.info(
                                    "Autopilot: NOT rescuing %s — score %d < "
                                    "RESCUE_MIN_SCORE %d (confident mismatch) — %s",
                                    v["id"], score, RESCUE_MIN_SCORE, v["title"],
                                )
                            mark_skipped(v["id"])
                            _stats["auto_skipped"] += 1
                            await asyncio.sleep(VACANCY_PAUSE)
                            continue

                    # Survived the skip gate -> SURFACED. Count against the
                    # per-feed surface quota (MAX_VACANCIES_PER_CYCLE = surfaced
                    # cards, not raw analyses).
                    processed += 1

                    # Surfaced — route to the best résumé variant NOW, after the
                    # skip gate, so the router runs only on vacancies the user
                    # will actually see. The row was saved above without a
                    # variant; persist the choice so the apply button, the letter
                    # register and the apply-modal selection all agree.
                    variant_key, _vr = await select_resume_variant(v, source_variant)
                    v["resume_variant"] = variant_key
                    set_vacancy_variant(v["id"], variant_key)

                    if AUTO_APPLY_ENABLED and score >= SCORE_AUTO_APPLY:
                        success = await auto_apply(hh_client, v)
                        if not success:
                            _stats["errors"] += 1

                    else:
                        # Manual-review card: auto-apply frozen, or score in
                        # the mid band, or a low score rescued by profile.
                        _stats["manual_review"] += 1
                        from bot.keyboards import vacancy_keyboard
                        rating_str = (
                            f"Рейтинг компании: {v.get('company_rating'):.1f}/5\n"
                            if v.get("company_rating") else ""
                        )
                        # Profile-marker line — shown whenever any marker hit,
                        # so you see which parts of your profile it touches.
                        profile_line = (
                            f"🎯 Профиль: {', '.join(matched_cats)} (overlap={overlap})\n"
                            if matched_cats else ""
                        )
                        # Header priority: would-be auto-apply (90+) first,
                        # then strong profile match, then analyzer relevance.
                        if score >= SCORE_AUTO_APPLY:
                            header = "🔥 СИЛЬНОЕ"
                        elif strong_profile:
                            header = "🎯 ПО ПРОФИЛЮ"
                        else:
                            header = {"high": "[!!!]", "medium": "[!!]", "low": "[!]"}.get(
                                v.get("relevance", ""), "[?]"
                            )
                        if rescued:
                            # Low analyzer score but on-profile — explain so a
                            # 10/100 doesn't read as "skip me".
                            score_line = (
                                f"analyzer={score}/100 — низкий (режется по "
                                f"role-type гейту), поднято по маркерам твоего "
                                f"профиля\n"
                            )
                        else:
                            reason = v.get("reason", "")
                            score_line = (
                                f"Релевантность: {score}/100\n"
                                + (f"Причина: {reason}\n" if reason else "")
                            )
                        text = (
                            f"{header} {v['title']}\n"
                            f"{v.get('company', '')}\n"
                            f"Зарплата: {v.get('salary') or 'не указана'}\n"
                            f"Город: {v.get('city') or ''}\n"
                            f"{rating_str}"
                            f"{profile_line}"
                            f"{score_line}"
                        )
                        text += f"Источник: {source_label}"
                        await bot.send_message(
                            TELEGRAM_ADMIN_ID, text,
                            reply_markup=vacancy_keyboard(
                                v["id"], v["url"], v.get("resume_variant"),
                            ),
                        )

                await asyncio.sleep(VACANCY_PAUSE)

        except Exception as e:
            logger.error("Autopilot search error for source %s: %s", source_label, e)
            _stats["errors"] += 1

    # also check previously found 90+ vacancies without response.
    # Capped at MAX_VACANCIES_PER_CYCLE so the per-cycle request volume stays
    # bounded — without this, a large backlog (e.g. 27 score-90 rows) gets
    # hammered in one cycle, which on a flagged/slow proxy IP both crawls and
    # feeds the anti-bot. Remaining backlog is picked up next cycle.
    # Backlog auto-apply runs only when auto-apply is enabled. Frozen by
    # default (AUTO_APPLY_ENABLED) — when off, nothing is applied automatically,
    # everything waits for manual review.
    if not AUTO_APPLY_ENABLED:
        return
    candidates = get_auto_apply_candidates()
    applied_ids = {v["id"] for v in _stats["auto_applied"]}
    backlog_done = 0
    for v in candidates:
        if v["id"] in applied_ids:
            continue
        if backlog_done >= MAX_VACANCIES_PER_CYCLE:
            logger.info(
                "Autopilot: backlog auto-apply cap (%d) reached this cycle, "
                "%d candidate(s) deferred to next cycle",
                MAX_VACANCIES_PER_CYCLE, len(candidates) - backlog_done,
            )
            break
        try:
            success = await auto_apply(hh_client, v)
            if not success:
                _stats["errors"] += 1
        except Exception as e:
            logger.error("Autopilot auto-apply error for %s: %s", v["id"], e)
            _stats["errors"] += 1
        backlog_done += 1
        await asyncio.sleep(VACANCY_PAUSE)


def _is_blacklisted(vacancy: dict) -> tuple[bool, str | None]:
    """Defensive guard: re-check company/title blacklist at apply time.

    Why duplicated from run_search_cycle: vacancies can arrive at
    auto_apply via TWO paths — the live search loop (where the checks
    above run) AND `get_auto_apply_candidates` (DB rows that may have been
    scored under an older, weaker blacklist). The second path bypassed those
    checks entirely; this guard closes that hole.
    """
    company_lower = (vacancy.get("company") or "").lower()
    if any(bl in company_lower for bl in COMPANY_BLACKLIST):
        return True, f"company blacklist: {vacancy.get('company')}"

    title_lower = (vacancy.get("title") or "").lower()
    matched = next((bl for bl in TITLE_BLACKLIST if bl in title_lower), None)
    if matched:
        return True, f"title blacklist match: '{matched}' in '{vacancy.get('title')}'"

    return False, None


async def auto_apply(hh_client: HHClient, vacancy: dict) -> bool:
    """Generate cover letter and apply to vacancy."""
    # Defensive: re-check blacklist even for vacancies pulled from DB
    # via get_auto_apply_candidates (which doesn't know about blacklists).
    blocked, reason = _is_blacklisted(vacancy)
    if blocked:
        logger.warning(
            "Auto-apply BLOCKED for %s (%s) — %s",
            vacancy.get("id"), vacancy.get("title"), reason,
        )
        mark_skipped(vacancy["id"])
        return False

    try:
        letter = await generate_cover_letter(vacancy)
        if not letter:
            logger.warning("Auto-apply: empty cover letter for %s", vacancy["id"])
            return False

        # Select the résumé tied to the routed variant in the apply modal
        # (None leaves hh's default résumé selected).
        _variant = vacancy.get("resume_variant")
        resume_identifier = apply_resume_for(_variant)
        resume_hash = apply_resume_hash_for(_variant)
        async with HH_LOCK:
            success = await hh_client.apply_to_vacancy(
                vacancy["id"], letter,
                resume_identifier=resume_identifier, resume_hash=resume_hash,
            )
        if success:
            save_response(vacancy["id"], letter, "sent")
            _stats["auto_applied"].append({
                "id": vacancy["id"],
                "title": vacancy.get("title", "?"),
                "company": vacancy.get("company", "?"),
                "score": vacancy.get("relevance_score", 0),
                "url": vacancy.get("url", ""),
            })
            log_action("auto_applied", f"vacancy={vacancy['id']}")
            return True
        else:
            return False
    except Exception as e:
        logger.error("Auto-apply error for %s: %s", vacancy["id"], e)
        return False


async def send_summary(bot: Bot):
    """Send accumulated summary to Telegram."""
    applied = _stats["auto_applied"]

    if not _stats["found"] and not applied:
        # nothing happened, don't spam
        _reset_stats()
        return

    applied_text = ""
    if applied:
        for v in applied:
            applied_text += f"\n{v['title']} @ {v['company']} ({v['score']}/100)\n{v['url']}"
    else:
        applied_text = "\nнет"

    now = datetime.now().strftime("%H:%M %d.%m")
    text = (
        f"Автопилот [{now}]\n\n"
        f"Новых вакансий: {_stats['found']}\n"
        f"Автооткликов ({SCORE_AUTO_APPLY}+): {len(applied)}{applied_text}\n\n"
        f"На ручной просмотр: {_stats['manual_review']}\n"
        f"Автопропуск: {_stats['auto_skipped']}\n"
        f"Ошибки: {_stats['errors']}"
    )

    try:
        await bot.send_message(TELEGRAM_ADMIN_ID, text)
    except Exception as e:
        logger.error("Summary error: %s", e)

    _reset_stats()
    log_action("autopilot_summary", f"applied={len(applied)}")
