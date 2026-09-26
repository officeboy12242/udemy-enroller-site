"""Database layer: MongoDB (pymongo).

Integer ids are kept (via a `counters` collection) so routes and templates that
pass numeric account/user ids keep working exactly as before - the storage
engine changed, the model function signatures did not. Credentials come from
MONGODB_URI / MONGODB_DB in the environment.
"""
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()
_client = None
_db = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_lock() -> threading.Lock:
    return _lock


def _connect():
    import certifi
    from pymongo import MongoClient
    if not config.MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set - configure it in .env")
    common = dict(serverSelectionTimeoutMS=12000, connectTimeoutMS=12000,
                  socketTimeoutMS=20000, retryWrites=True, retryReads=True)
    try:
        c = MongoClient(config.MONGODB_URI, tls=True, tlsCAFile=certifi.where(), **common)
        c.admin.command("ping")
        return c
    except Exception:
        # Fallback for environments with awkward CA chains (mirrors tgbot2).
        c = MongoClient(config.MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True, **common)
        c.admin.command("ping")
        return c


def get_db():
    global _client, _db
    if _db is None:
        with _lock:
            if _db is None:
                _client = _connect()
                _db = _client[config.MONGODB_DB]
                _ensure_indexes(_db)
    return _db


def _ensure_indexes(db) -> None:
    from pymongo import ASCENDING, DESCENDING
    db.users.create_index([("email", ASCENDING)], unique=True)
    db.accounts.create_index([("user_id", ASCENDING)])
    # One Udemy identity per site user; ignore docs without a udemy_user_id.
    db.accounts.create_index(
        [("udemy_user_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"udemy_user_id": {"$type": "number"}},
        name="uniq_udemy_user",
    )
    db.enrollment_log.create_index([("account_id", ASCENDING), ("slug", ASCENDING)],
                                   unique=True, name="uniq_account_slug")
    db.enrollment_log.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])


def next_id(name: str) -> int:
    """Atomic auto-increment integer id for a collection."""
    from pymongo import ReturnDocument
    doc = get_db().counters.find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def with_id(doc: dict | None) -> dict | None:
    """Expose Mongo `_id` as `id` for callers that expect the old integer id."""
    if not doc:
        return None
    d = dict(doc)
    if "_id" in d:
        d["id"] = d.pop("_id")
    return d
