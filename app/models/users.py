"""Site user accounts (not Udemy accounts). MongoDB-backed."""
from .. import config, security
from ..db import get_db, next_id, now_iso, with_id


def create_user(email: str, password: str, is_admin: bool = False,
                password_hash: str | None = None) -> int:
    db = get_db()
    uid = next_id("users")
    db.users.insert_one({
        "_id": uid,
        "email": email.strip().lower(),
        "password_hash": password_hash if password_hash is not None else security.hash_password(password),
        "is_admin": int(is_admin),
        "created_at": now_iso(),
    })
    db.auto_state.update_one(
        {"_id": uid},
        {"$setOnInsert": {"enabled": int(config.AUTO_ENROLL_ENABLED_DEFAULT), "running": 0,
                          "last_run": None, "next_run": None, "last_result": None,
                          "total_enrolled": 0, "updated_at": None}},
        upsert=True,
    )
    return uid


def get_user_by_email(email: str):
    return with_id(get_db().users.find_one({"email": email.strip().lower()}))


def get_user(user_id: int):
    return with_id(get_db().users.find_one({"_id": user_id}))


def count_users() -> int:
    return get_db().users.count_documents({})
