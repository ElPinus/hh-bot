import sqlite3
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vacancies (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            city TEXT,
            url TEXT NOT NULL,
            description TEXT,
            relevance TEXT,
            relevance_score INTEGER DEFAULT 0,
            company_rating REAL DEFAULT 0,
            found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id TEXT NOT NULL,
            cover_letter TEXT,
            status TEXT DEFAULT 'sent',
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
        );

        CREATE TABLE IF NOT EXISTS filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            keywords TEXT,
            city TEXT,
            salary_from INTEGER,
            salary_to INTEGER,
            experience TEXT,
            schedule TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_url TEXT,
            vacancy_id TEXT,
            vacancy_title TEXT,
            employer TEXT,
            preview TEXT,
            content_hash TEXT NOT NULL,
            is_test_task INTEGER DEFAULT 0,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_url, content_hash)
        );

        CREATE TABLE IF NOT EXISTS employers (
            id TEXT PRIMARY KEY,
            rating REAL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Dedup ledger for Telegram channel posts. Every post we pull is
        -- recorded here (vacancy or not) so the monitoring loop never
        -- re-runs the LLM extractor on a post it already processed. Kept
        -- separate from `vacancies` so non-vacancy posts (news, ads,
        -- digests) don't pollute vacancy stats or get picked up by /recheck.
        CREATE TABLE IF NOT EXISTS tg_seen (
            post_id TEXT PRIMARY KEY,
            channel TEXT,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Lightweight migrations for tables that pre-date later columns.
    _ensure_column(conn, "vacancies", "company_rating", "REAL DEFAULT 0")
    # source distinguishes hh.ru vacancies from Telegram-sourced ones.
    # Pre-existing rows default to 'hh' (they all came from hh.ru).
    _ensure_column(conn, "vacancies", "source", "TEXT DEFAULT 'hh'")
    # search_field lets a filter restrict hh search to the vacancy title
    # ("name") instead of full text — used to catch AI/ИИ in titles without
    # flooding on description mentions. Empty = hh default (search everywhere).
    _ensure_column(conn, "filters", "search_field", "TEXT DEFAULT ''")
    # resume_variant records which configured résumé the bot routed this
    # vacancy to (key matching ai/resume_variants.py). Drives the letter
    # register and the résumé picked in the apply modal. NULL on pre-existing
    # rows / when routing hasn't run yet (callers fall back to the default).
    _ensure_column(conn, "vacancies", "resume_variant", "TEXT")
    conn.close()


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN if missing — SQLite has no IF NOT EXISTS for this."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
