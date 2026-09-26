"""Database layer: single shared SQLite connection (WAL), schema, migrations.

Models in app/models/ build on the helpers here. Keeping connection + schema +
migrations in one place makes the data layer easy to reason about and evolve.
"""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

DB_PATH = Path(config.DATA_DIR) / "enroller.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    udemy_user_id    INTEGER,
    udemy_name       TEXT,
    access_token_enc TEXT NOT NULL,
    client_id        TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    auto_enroll      INTEGER NOT NULL DEFAULT 1,
    total_courses    INTEGER,
    total_courses_updated_at TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(user_id, udemy_user_id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE TABLE IF NOT EXISTS enrollment_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    account_id      INTEGER NOT NULL,
    udemy_course_id TEXT,
    slug            TEXT,
    title           TEXT,
    course_url      TEXT,
    image           TEXT,
    original_price  REAL,
    currency        TEXT,
    category        TEXT,
    language        TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      TEXT NOT NULL,
    UNIQUE(account_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_enroll_user_time ON enrollment_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_enroll_account ON enrollment_log(account_id);
CREATE TABLE IF NOT EXISTS auto_state (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled         INTEGER NOT NULL DEFAULT 0,
    running         INTEGER NOT NULL DEFAULT 0,
    last_run        TEXT,
    next_run        TEXT,
    last_result     TEXT,
    total_enrolled  INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS enroll_prefs (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    languages       TEXT NOT NULL DEFAULT '[]',
    categories      TEXT NOT NULL DEFAULT '[]',
    min_rating      REAL NOT NULL DEFAULT 0,
    include_unrated INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS account_prefs (
    account_id      INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    languages       TEXT NOT NULL DEFAULT '[]',
    categories      TEXT NOT NULL DEFAULT '[]',
    min_rating      REAL NOT NULL DEFAULT 0,
    include_unrated INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_lock() -> threading.Lock:
    return _lock


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent, additive migrations for pre-existing databases."""
    acc_cols = _column_names(conn, "accounts")
    if "total_courses" not in acc_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN total_courses INTEGER")
    if "total_courses_updated_at" not in acc_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN total_courses_updated_at TEXT")

    cols = _column_names(conn, "enrollment_log")
    if "image" not in cols:
        conn.execute("ALTER TABLE enrollment_log ADD COLUMN image TEXT")
    if "original_price" not in cols:
        conn.execute("ALTER TABLE enrollment_log ADD COLUMN original_price REAL")
    if "currency" not in cols:
        conn.execute("ALTER TABLE enrollment_log ADD COLUMN currency TEXT")
    if "category" not in cols:
        conn.execute("ALTER TABLE enrollment_log ADD COLUMN category TEXT")
    if "language" not in cols:
        conn.execute("ALTER TABLE enrollment_log ADD COLUMN language TEXT")
    conn.commit()


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
        _run_migrations(_conn)
    return _conn
