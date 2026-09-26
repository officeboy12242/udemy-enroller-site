"""User accounts on this site (not Udemy accounts)."""
from .. import config, security
from ..db import get_db, db_lock, now_iso


def create_user(email: str, password: str, is_admin: bool = False) -> int:
    lock = db_lock()
    with lock:
        db = get_db()
        cur = db.execute(
            "INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (email.strip().lower(), security.hash_password(password), int(is_admin), now_iso()),
        )
        db.commit()
        uid = cur.lastrowid
        db.execute(
            "INSERT OR IGNORE INTO auto_state (user_id, enabled) VALUES (?,?)",
            (uid, int(config.AUTO_ENROLL_ENABLED_DEFAULT)),
        )
        db.commit()
    return uid


def get_user_by_email(email: str):
    row = get_db().execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def get_user(user_id: int):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    return get_db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
