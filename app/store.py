"""SQLite persistence: users, Udemy accounts (encrypted tokens), enrollment log, auto-enroll state."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, security

DB_PATH = Path(config.DATA_DIR) / "enroller.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(user_id, udemy_user_id)
        );
        CREATE TABLE IF NOT EXISTS enrollment_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            account_id     INTEGER NOT NULL,
            udemy_course_id TEXT,
            slug           TEXT,
            title          TEXT,
            course_url     TEXT,
            source         TEXT NOT NULL DEFAULT 'manual',
            created_at     TEXT NOT NULL,
            UNIQUE(account_id, slug)
        );
        CREATE INDEX IF NOT EXISTS idx_enroll_user_time ON enrollment_log(user_id, created_at DESC);
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
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(email: str, password: str, is_admin: bool = False) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (email.strip().lower(), security.hash_password(password), int(is_admin), _now()),
        )
        _db().commit()
        uid = cur.lastrowid
    _db().execute(
        "INSERT OR IGNORE INTO auto_state (user_id, enabled) VALUES (?,?)",
        (uid, int(config.AUTO_ENROLL_ENABLED_DEFAULT)),
    )
    _db().commit()
    return uid


def get_user_by_email(email: str):
    row = _db().execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def get_user(user_id: int):
    row = _db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    return _db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


# ── Udemy accounts ──────────────────────────────────────────────────────────

def upsert_account(
    user_id: int,
    access_token: str,
    client_id: str,
    udemy_user_id: int | None,
    udemy_name: str | None,
) -> int:
    """Create or refresh the account record for this Udemy identity. Returns account id."""
    now = _now()
    enc = security.encrypt_secret(access_token)
    with _lock:
        db = _db()
        row = db.execute(
            "SELECT id FROM accounts WHERE user_id=? AND udemy_user_id=?",
            (user_id, udemy_user_id),
        ).fetchone() if udemy_user_id is not None else None
        if row:
            db.execute(
                "UPDATE accounts SET access_token_enc=?, client_id=?, udemy_name=?, updated_at=? WHERE id=?",
                (enc, client_id, udemy_name, now, row["id"]),
            )
            acc_id = row["id"]
        else:
            # One Udemy account may only be linked to one site user.
            dup = db.execute(
                "SELECT user_id FROM accounts WHERE udemy_user_id=? AND user_id<>?",
                (udemy_user_id, user_id),
            ).fetchone() if udemy_user_id is not None else None
            if dup:
                raise ValueError("This Udemy account is already linked to another site user.")
            cur = db.execute(
                """INSERT INTO accounts
                   (user_id, udemy_user_id, udemy_name, access_token_enc, client_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, udemy_user_id, udemy_name, enc, client_id, now, now),
            )
            acc_id = cur.lastrowid
        db.commit()
    return acc_id


def get_accounts(user_id: int, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM accounts WHERE user_id=?"
    if active_only:
        q += " AND is_active=1"
    q += " ORDER BY id"
    out = []
    for r in _db().execute(q, (user_id,)).fetchall():
        d = dict(r)
        try:
            d["access_token"] = security.decrypt_secret(d.pop("access_token_enc"))
        except Exception:
            d["access_token"] = ""
        out.append(d)
    return out


def get_account_by_id(account_id: int) -> dict | None:
    r = _db().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["access_token"] = security.decrypt_secret(d.pop("access_token_enc"))
    except Exception:
        d["access_token"] = ""
    return d


def get_all_active_auto_accounts() -> list[dict]:
    """All accounts (any user) with auto_enroll on — used by the background engine."""
    rows = _db().execute(
        "SELECT a.*, u.email AS site_email FROM accounts a JOIN users u ON u.id=a.user_id "
        "WHERE a.is_active=1 AND a.auto_enroll=1 ORDER BY a.id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["access_token"] = security.decrypt_secret(d.pop("access_token_enc"))
        except Exception:
            d["access_token"] = ""
        out.append(d)
    return out


def set_account_flags(account_id: int, is_active: bool = None, auto_enroll: bool = None) -> None:
    sets, vals = [], []
    if is_active is not None:
        sets.append("is_active=?"); vals.append(int(is_active))
    if auto_enroll is not None:
        sets.append("auto_enroll=?"); vals.append(int(auto_enroll))
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(_now()); vals.append(account_id)
    with _lock:
        _db().execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
        _db().commit()


def delete_account(account_id: int, user_id: int) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user_id))
        _db().commit()
    return cur.rowcount > 0


# ── Enrollment log ──────────────────────────────────────────────────────────

def log_enrollment(user_id: int, account_id: int, course_id, slug, title, course_url, source: str) -> bool:
    """Insert enrollment; returns False if this slug was already logged for the account."""
    try:
        with _lock:
            _db().execute(
                """INSERT INTO enrollment_log (user_id, account_id, udemy_course_id, slug, title, course_url, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (user_id, account_id, str(course_id or ""), slug or "", title or "", course_url or "", source, _now()),
            )
            _db().commit()
        return True
    except sqlite3.IntegrityError:
        return False


def is_enrolled_by_slug(account_id: int, slug: str) -> bool:
    return _db().execute(
        "SELECT 1 FROM enrollment_log WHERE account_id=? AND slug=?", (account_id, slug)
    ).fetchone() is not None


def get_enrolled_slugs(account_id: int) -> set[str]:
    return {
        r["slug"]
        for r in _db().execute(
            "SELECT slug FROM enrollment_log WHERE account_id=? AND slug<>''", (account_id,)
        ).fetchall()
        if r["slug"]
    }


def get_history(user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM enrollment_log WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def count_enrollments(user_id: int, since: str | None = None) -> int:
    if since:
        row = _db().execute(
            "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=? AND created_at>=?", (user_id, since)
        ).fetchone()
    else:
        row = _db().execute(
            "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["c"]


# ── Auto-enroll state ───────────────────────────────────────────────────────

def get_auto_state(user_id: int) -> dict:
    row = _db().execute("SELECT * FROM auto_state WHERE user_id=?", (user_id,)).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": user_id, "enabled": int(config.AUTO_ENROLL_ENABLED_DEFAULT),
        "running": 0, "last_run": None, "next_run": None,
        "last_result": None, "total_enrolled": 0, "updated_at": None,
    }


def set_auto_enabled(user_id: int, enabled: bool) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO auto_state (user_id, enabled, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (user_id, int(enabled), _now()),
        )
        _db().commit()


def set_auto_running(user_id: int, running: bool) -> None:
    with _lock:
        _db().execute("UPDATE auto_state SET running=? WHERE user_id=?", (int(running), user_id))
        _db().commit()


def update_auto_run(user_id: int, last_result: str, enrolled_count: int, next_run_iso: str | None) -> None:
    with _lock:
        _db().execute(
            """UPDATE auto_state
               SET last_run=?, next_run=?, last_result=?, total_enrolled=total_enrolled+?, running=0, updated_at=?
               WHERE user_id=?""",
            (_now(), next_run_iso, last_result, enrolled_count, _now(), user_id),
        )
        _db().commit()


# ── Global settings ─────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    row = _db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        _db().commit()
