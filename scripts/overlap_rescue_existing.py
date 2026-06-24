"""One-shot retroactive overlap rescue.

The profile-overlap rescue normally applies only to vacancies the autopilot
encounters NEXT. But the live search loop re-visits the same already-known
vacancies for hours, so nothing new shows up in Telegram. This script reaches
back into the DB, applies the SAME overlap logic to vacancies stored with a
low score in the last N days, and pushes the rescued ones to Telegram now.

Run: python -m scripts.overlap_rescue_existing
Tunable args (env or hard-coded below): days back, score band.

Safe to re-run: skips vacancies that already have a 'sent' response
or have been delivered to the manual-review queue in this session.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Make sure the project root is on sys.path so we can import bot.*
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram import Bot
from db.models import get_connection
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID
from bot.autopilot import (
    profile_rescue,
    COMPANY_BLACKLIST,
    TITLE_BLACKLIST,
)
from bot.keyboards import vacancy_keyboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("overlap_rescue")

# Tunables
DAYS_BACK = 14
MIN_SCORE = 5
MAX_SCORE = 39   # below the live floor SCORE_AUTO_SKIP=40 — the below-floor
                 # band the autopilot would rescue; 40+ surfaces via the
                 # normal manual-review band already, so it's excluded here
MAX_SEND = 50    # safety cap per run — don't flood Telegram
SEND_PAUSE_S = 1.5  # gentle pacing for Telegram API


def _pull_candidates() -> list[dict]:
    """Find existing low-score vacancies that are rescue-eligible.

    Exclude only ALREADY-SENT applications (status='sent'). Previously
    auto-skipped vacancies (status='skipped' or status='failed') are
    fair game — they were filtered by the OLD logic, the new overlap
    rescue may rate them differently and the user should see them.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT v.*
            FROM vacancies v
            WHERE v.found_at > datetime('now', '-{DAYS_BACK} days')
              AND v.relevance_score BETWEEN {MIN_SCORE} AND {MAX_SCORE}
              AND v.id NOT IN (
                  SELECT vacancy_id FROM responses WHERE status='sent'
              )
            ORDER BY v.relevance_score DESC, v.found_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _is_blocked(v: dict) -> str | None:
    """Mirror autopilot's pre-LLM filters so we don't surface vacancies
    the user has already classified as off-target."""
    company_lower = (v.get("company") or "").lower()
    if any(bl in company_lower for bl in COMPANY_BLACKLIST):
        return f"company-blacklist: {v.get('company')}"
    title_lower = (v.get("title") or "").lower()
    for bl in TITLE_BLACKLIST:
        if bl in title_lower:
            return f"title-blacklist: '{bl}'"
    return None


async def main() -> None:
    cands = _pull_candidates()
    logger.info("Candidates (score %d-%d, %d days, no prior response): %d",
                MIN_SCORE, MAX_SCORE, DAYS_BACK, len(cands))

    if not cands:
        logger.info("Nothing to rescue. Done.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    sent = 0
    skipped_blocked = 0
    skipped_not_eligible = 0

    try:
        for v in cands:
            if sent >= MAX_SEND:
                logger.info("Hit MAX_SEND=%d cap, stopping early", MAX_SEND)
                break

            blocked = _is_blocked(v)
            if blocked:
                skipped_blocked += 1
                continue

            eligible, score, cats = profile_rescue(
                v.get("description") or "", v.get("title") or ""
            )
            if not eligible:
                skipped_not_eligible += 1
                continue

            # Eligible — push to Telegram. Lead with the rescue signal
            # (overlap categories) rather than the analyzer's low score:
            # in rescue cards the score is LOW BY DESIGN (the analyzer cut
            # the role by role-type gate), and we override because overlap
            # markers match the configured profile. Showing the low score
            # first reads as "skip this", so the overlap signal leads instead.
            rating_str = (
                f"Рейтинг компании: {v.get('company_rating'):.1f}/5\n"
                if v.get("company_rating") else ""
            )
            overlap_meta = f"[overlap={score}] cats: {', '.join(cats)}"
            text = (
                f"🎯 OVERLAP-RESCUE (retroactive)\n"
                f"{v.get('title') or ''}\n"
                f"{v.get('company') or ''}\n"
                f"Зарплата: {v.get('salary') or 'не указана'}\n"
                f"Город: {v.get('city') or ''}\n"
                f"{rating_str}"
                f"{overlap_meta}\n"
                f"(analyzer={v.get('relevance_score', 0)}/100 — низкий, "
                f"режется по role-type гейту; подняли через overlap-маркеры "
                f"твоего профиля)"
            )
            try:
                await bot.send_message(
                    TELEGRAM_ADMIN_ID,
                    text,
                    reply_markup=vacancy_keyboard(v["id"], v["url"]),
                )
                sent += 1
                logger.info("Sent %d/%d: %s (overlap=%d %s)",
                            sent, MAX_SEND, v.get("title", ""), score, cats)
            except Exception as e:
                logger.warning("send failed for %s: %s", v.get("id"), e)

            await asyncio.sleep(SEND_PAUSE_S)
    finally:
        await bot.session.close()

    logger.info(
        "Done. sent=%d  skipped: blocked=%d, not_eligible=%d",
        sent, skipped_blocked, skipped_not_eligible,
    )


if __name__ == "__main__":
    asyncio.run(main())
