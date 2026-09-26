"""Per-user auto-enroll engine state."""
from .. import config
from ..db import get_db, db_lock, now_iso


def get_auto_state(user_id: int) -> dict:
    row = get_db().execute("SELECT * FROM auto_state WHERE user_id=?", (user_id,)).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": user_id, "enabled": int(config.AUTO_ENROLL_ENABLED_DEFAULT),
        "running": 0, "last_run": None, "next_run": None,
        "last_result": None, "total_enrolled": 0, "updated_at": None,
    }


def set_auto_enabled(user_id: int, enabled: bool) -> None:
    with db_lock():
        db = get_db()
        db.execute(
            "INSERT INTO auto_state (user_id, enabled, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (user_id, int(enabled), now_iso()),
        )
        db.commit()


def set_auto_running(user_id: int, running: bool) -> None:
    with db_lock():
        db = get_db()
        db.execute("UPDATE auto_state SET running=? WHERE user_id=?", (int(running), user_id))
        db.commit()


def update_auto_run(user_id: int, last_result: str, enrolled_count: int, next_run_iso: str | None) -> None:
    with db_lock():
        db = get_db()
        db.execute(
            """UPDATE auto_state
               SET last_run=?, next_run=?, last_result=?, total_enrolled=total_enrolled+?, running=0, updated_at=?
               WHERE user_id=?""",
            (now_iso(), next_run_iso, last_result, enrolled_count, now_iso(), user_id),
        )
        db.commit()


def users_with_auto_enabled() -> list[int]:
    rows = get_db().execute("SELECT user_id FROM auto_state WHERE enabled=1 AND running=0").fetchall()
    return [r["user_id"] for r in rows]
