"""Per-user auto-enroll engine state. MongoDB-backed."""
from .. import config
from ..db import get_db, now_iso


def get_auto_state(user_id: int) -> dict:
    doc = get_db().auto_state.find_one({"_id": user_id})
    if doc:
        d = dict(doc); d["user_id"] = d.pop("_id"); return d
    return {
        "user_id": user_id, "enabled": int(config.AUTO_ENROLL_ENABLED_DEFAULT),
        "running": 0, "last_run": None, "next_run": None,
        "last_result": None, "total_enrolled": 0, "updated_at": None,
    }


def set_auto_enabled(user_id: int, enabled: bool) -> None:
    get_db().auto_state.update_one(
        {"_id": user_id},
        {"$set": {"enabled": int(enabled), "updated_at": now_iso()}},
        upsert=True,
    )


def set_auto_running(user_id: int, running: bool) -> None:
    get_db().auto_state.update_one({"_id": user_id}, {"$set": {"running": int(running)}}, upsert=True)


def update_auto_run(user_id: int, last_result: str, enrolled_count: int, next_run_iso: str | None) -> None:
    get_db().auto_state.update_one(
        {"_id": user_id},
        {"$set": {"last_run": now_iso(), "next_run": next_run_iso, "last_result": last_result,
                  "running": 0, "updated_at": now_iso()},
         "$inc": {"total_enrolled": int(enrolled_count)}},
        upsert=True,
    )


def users_with_auto_enabled() -> list[int]:
    return [d["_id"] for d in get_db().auto_state.find({"enabled": 1, "running": {"$ne": 1}}, {"_id": 1})]
