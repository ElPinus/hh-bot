import re

from db.models import get_connection


def _normalize_title(title: str) -> str:
    """Normalize vacancy title for cross-city duplicate detection.

    Drops parenthetical mentions like "(Москва)", "(remote)", "(в Москве)"
    that often vary across duplicate postings of the same role; lowercases;
    collapses whitespace. Keeps slash-separated alternatives intact since
    they're usually meaningful role variants ("Backend Developer / Engineer").
    """
    if not title:
        return ""
    t = title.lower()
    # drop parenthetical bits — typical pattern for city/remote variants
    t = re.sub(r"\([^)]*\)", "", t)
    # collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_duplicate_already_handled(company: str, title: str) -> bool:
    """Return True if the same employer already has a vacancy with the same
    normalized title that is either:
      - already responded to (sent or skipped), or
      - sitting at relevance_score >= 50 with no response yet (pending manual
        review — duplicate would re-notify Telegram with the same content).

    Designed to filter cross-city / cross-format duplicates that some
    employers post separately ("Developer (Москва)", "Developer (СПб)",
    "Developer (удалёнка)" — same role, three vacancy ids).
    """
    norm = _normalize_title(title)
    company_lower = (company or "").strip().lower()
    if not norm or not company_lower:
        return False

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT v.title, v.relevance_score,
                      (SELECT status FROM responses WHERE vacancy_id = v.id LIMIT 1)
                        AS resp_status
               FROM vacancies v
               WHERE LOWER(v.company) = ?""",
            (company_lower,),
        ).fetchall()
        for row in rows:
            if _normalize_title(row["title"]) != norm:
                continue
            resp_status = row["resp_status"]
            if resp_status in ("sent", "skipped"):
                return True
            score = row["relevance_score"] or 0
            if score >= 50 and resp_status is None:
                # already queued to manual review — no point in another ping
                return True
        return False
    finally:
        conn.close()


def save_vacancy(vacancy: dict) -> bool:
    """Save vacancy, return True if new."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO vacancies
               (id, title, company, salary, city, url, description, relevance, relevance_score, company_rating, source, resume_variant)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vacancy["id"],
                vacancy["title"],
                vacancy.get("company", ""),
                vacancy.get("salary", ""),
                vacancy.get("city", ""),
                vacancy["url"],
                vacancy.get("description", ""),
                vacancy.get("relevance", ""),
                vacancy.get("relevance_score", 0),
                vacancy.get("company_rating", 0) or 0,
                vacancy.get("source", "hh"),
                vacancy.get("resume_variant"),
            ),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def vacancy_exists(vacancy_id: str) -> bool:
    """True if this vacancy was already saved (examined) in a past cycle.

    The autopilot uses this to skip re-fetching + re-analyzing vacancies it
    has already processed: the resume search re-returns the same freshest
    vacancies every cycle, so without an early skip each one would be opened
    (proxy goto) and scored (LLM call) again — at a short cadence that would
    flood the LLM and trip hh.ru's anti-bot on the proxy IP. New vacancies
    (not yet in the table) fall through and get processed.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM vacancies WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_unseen_vacancies(limit: int = 10) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT v.* FROM vacancies v
               LEFT JOIN responses r ON v.id = r.vacancy_id
               WHERE r.id IS NULL
               ORDER BY v.relevance_score DESC, v.found_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_response(vacancy_id: str, cover_letter: str, status: str = "sent"):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO responses (vacancy_id, cover_letter, status) VALUES (?, ?, ?)",
            (vacancy_id, cover_letter, status),
        )
        conn.commit()
    finally:
        conn.close()


def mark_skipped(vacancy_id: str):
    save_response(vacancy_id, "", "skipped")


def get_stats() -> dict:
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM responses WHERE status='sent'"
        ).fetchone()[0]
        skipped = conn.execute(
            "SELECT COUNT(*) FROM responses WHERE status='skipped'"
        ).fetchone()[0]
        pending = total - sent - skipped
        return {
            "total": total,
            "sent": sent,
            "skipped": skipped,
            "pending": pending,
        }
    finally:
        conn.close()


def save_filter(filter_data: dict):
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO filters (name, keywords, city, salary_from, salary_to, experience, schedule)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                filter_data["name"],
                filter_data.get("keywords", ""),
                filter_data.get("city", ""),
                filter_data.get("salary_from"),
                filter_data.get("salary_to"),
                filter_data.get("experience", ""),
                filter_data.get("schedule", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_active_filters() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM filters WHERE is_active = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_unscored_vacancies() -> list[dict]:
    """Get vacancies with relevance_score = 0 (not yet analyzed by AI)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM vacancies WHERE relevance_score = 0"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_vacancy_relevance(vacancy_id: str, relevance: str, score: int, reason: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE vacancies SET relevance = ?, relevance_score = ? WHERE id = ?",
            (relevance, score, vacancy_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_vacancy_variant(vacancy_id: str, variant: str) -> None:
    """Store the résumé variant routed (or manually overridden) for a vacancy.
    The cover-letter register and the apply-modal résumé selection both honour
    this value."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE vacancies SET resume_variant = ? WHERE id = ?",
            (variant, vacancy_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_vacancy_variant(vacancy_id: str) -> str | None:
    """Return the stored resume_variant for a vacancy, or None if unset."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT resume_variant FROM vacancies WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return row["resume_variant"] if row else None
    finally:
        conn.close()


def get_auto_apply_candidates(min_score: int = 90) -> list[dict]:
    """Get vacancies with score >= min_score that haven't been responded to.

    Default `min_score` is intentionally aligned with the autopilot's
    `SCORE_AUTO_APPLY` constant so a single change of the threshold
    flows through. Don't lower this below the live threshold or the
    autopilot will retry vacancies it just decided to skip.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT v.* FROM vacancies v
               LEFT JOIN responses r ON v.id = r.vacancy_id
               WHERE r.id IS NULL AND v.relevance_score >= ?
               ORDER BY v.relevance_score DESC""",
            (min_score,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_action(action: str, details: str = ""):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO logs (action, details) VALUES (?, ?)",
            (action, details),
        )
        conn.commit()
    finally:
        conn.close()


def is_message_seen(chat_url: str, content_hash: str) -> bool:
    """Return True if we already notified about this message (or one with same hash)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE chat_url = ? AND content_hash = ?",
            (chat_url, content_hash),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_employer_rating_cached(
    employer_id: str, ttl_days: int = 30
) -> tuple[bool, float | None]:
    """Return (found, rating).

    found=True  -> there is a fresh cache entry; use the returned rating
                   (which may be None if the company has no rating on hh.ru).
    found=False -> need to fetch (no entry or expired).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT rating FROM employers "
            f"WHERE id = ? AND fetched_at >= datetime('now', '-{int(ttl_days)} days')",
            (employer_id,),
        ).fetchone()
        if row is None:
            return False, None
        return True, row["rating"]
    finally:
        conn.close()


def save_employer_rating(employer_id: str, rating: float | None):
    """Upsert employer rating + bump fetched_at."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO employers (id, rating, fetched_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (employer_id, rating),
        )
        conn.commit()
    finally:
        conn.close()


def tg_seen_exists(post_id: str) -> bool:
    """Return True if this Telegram post was already pulled and processed.

    Used by the TG monitoring loop to avoid re-running the LLM extractor
    on posts it has seen — channels keep a post in the lookback window
    across several pull cycles, so without this every post would be
    re-extracted (and re-billed) each cycle.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM tg_seen WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def tg_seen_mark(post_id: str, channel: str = "") -> None:
    """Record a Telegram post as processed (idempotent)."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tg_seen (post_id, channel) VALUES (?, ?)",
            (post_id, channel),
        )
        conn.commit()
    finally:
        conn.close()


def save_message(
    chat_url: str,
    content_hash: str,
    vacancy_id: str = "",
    vacancy_title: str = "",
    employer: str = "",
    preview: str = "",
    is_test_task: bool = False,
) -> bool:
    """Record a notified message. Returns True if newly inserted."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO messages
               (chat_url, vacancy_id, vacancy_title, employer, preview, content_hash, is_test_task)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                chat_url,
                vacancy_id,
                vacancy_title,
                employer,
                preview,
                content_hash,
                1 if is_test_task else 0,
            ),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()
