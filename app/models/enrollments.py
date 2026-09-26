"""Enrollment log (one row per course enrolled per account)."""
import sqlite3

from ..db import get_db, db_lock, now_iso


def log_enrollment(user_id: int, account_id: int, course_id, slug, title, course_url,
                   source: str, image: str | None = None,
                   original_price: float | None = None, currency: str | None = None,
                   category: str | None = None, language: str | None = None) -> bool:
    """Insert enrollment; returns False if this slug was already logged for the account."""
    try:
        with db_lock():
            db = get_db()
            db.execute(
                """INSERT INTO enrollment_log
                   (user_id, account_id, udemy_course_id, slug, title, course_url, image,
                    original_price, currency, category, language, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, account_id, str(course_id or ""), slug or "", title or "",
                 course_url or "", image or "", original_price, currency, category, language,
                 source, now_iso()),
            )
            db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def is_enrolled_by_slug(account_id: int, slug: str) -> bool:
    return get_db().execute(
        "SELECT 1 FROM enrollment_log WHERE account_id=? AND slug=?", (account_id, slug)
    ).fetchone() is not None


def get_enrolled_slugs(account_id: int) -> set[str]:
    return {
        r["slug"]
        for r in get_db().execute(
            "SELECT slug FROM enrollment_log WHERE account_id=? AND slug<>''", (account_id,)
        ).fetchall()
        if r["slug"]
    }


def get_history(user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM enrollment_log WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def count_enrollments(user_id: int, since: str | None = None) -> int:
    if since:
        row = get_db().execute(
            "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=? AND created_at>=?", (user_id, since)
        ).fetchone()
    else:
        row = get_db().execute(
            "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["c"]
