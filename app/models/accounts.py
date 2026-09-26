"""Linked Udemy accounts (access tokens encrypted at rest). MongoDB-backed."""
from .. import security
from ..db import get_db, next_id, now_iso, with_id


def _row_with_token(doc) -> dict:
    d = with_id(doc)
    try:
        d["access_token"] = security.decrypt_secret(d.pop("access_token_enc"))
    except Exception:
        d["access_token"] = ""
        d.pop("access_token_enc", None)
    return d


def upsert_account(user_id: int, access_token: str, client_id: str,
                   udemy_user_id: int | None, udemy_name: str | None) -> int:
    now = now_iso()
    enc = security.encrypt_secret(access_token)
    db = get_db()
    existing = db.accounts.find_one({"user_id": user_id, "udemy_user_id": udemy_user_id}) \
        if udemy_user_id is not None else None
    if existing:
        db.accounts.update_one({"_id": existing["_id"]}, {"$set": {
            "access_token_enc": enc, "client_id": client_id, "udemy_name": udemy_name,
            "is_active": 1, "updated_at": now,
        }})
        return existing["_id"]

    if udemy_user_id is not None:
        dup = db.accounts.find_one({"udemy_user_id": udemy_user_id, "user_id": {"$ne": user_id}})
        if dup:
            raise ValueError("This Udemy account is already linked to another site user.")

    acc_id = next_id("accounts")
    db.accounts.insert_one({
        "_id": acc_id, "user_id": user_id, "udemy_user_id": udemy_user_id,
        "udemy_name": udemy_name, "access_token_enc": enc, "client_id": client_id,
        "is_active": 1, "auto_enroll": 1, "total_courses": None,
        "total_courses_updated_at": None, "created_at": now, "updated_at": now,
    })
    return acc_id


def get_accounts(user_id: int, active_only: bool = False) -> list[dict]:
    q = {"user_id": user_id}
    if active_only:
        q["is_active"] = 1
    return [_row_with_token(d) for d in get_db().accounts.find(q).sort("_id", 1)]


def get_account_by_id(account_id: int) -> dict | None:
    d = get_db().accounts.find_one({"_id": account_id})
    return _row_with_token(d) if d else None


def get_all_active_auto_accounts() -> list[dict]:
    db = get_db()
    out = []
    for d in db.accounts.find({"is_active": 1, "auto_enroll": 1}).sort("_id", 1):
        row = _row_with_token(d)
        u = db.users.find_one({"_id": d["user_id"]}, {"email": 1})
        row["site_email"] = u["email"] if u else None
        out.append(row)
    return out


def set_account_flags(account_id: int, is_active: bool = None, auto_enroll: bool = None) -> None:
    sets = {}
    if is_active is not None:
        sets["is_active"] = int(is_active)
    if auto_enroll is not None:
        sets["auto_enroll"] = int(auto_enroll)
    if not sets:
        return
    sets["updated_at"] = now_iso()
    get_db().accounts.update_one({"_id": account_id}, {"$set": sets})


def set_account_total_courses(account_id: int, count: int) -> None:
    get_db().accounts.update_one({"_id": account_id}, {"$set": {
        "total_courses": count, "total_courses_updated_at": now_iso(),
    }})


def delete_account(account_id: int, user_id: int) -> bool:
    res = get_db().accounts.delete_one({"_id": account_id, "user_id": user_id})
    if res.deleted_count:
        get_db().account_prefs.delete_one({"_id": account_id})
    return res.deleted_count > 0
