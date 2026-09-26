"""Global key/value settings. MongoDB-backed."""
from ..db import get_db


def get_setting(key: str, default=None):
    doc = get_db().settings.find_one({"_id": key})
    return doc["value"] if doc else default


def set_setting(key: str, value: str) -> None:
    get_db().settings.update_one({"_id": key}, {"$set": {"value": str(value)}}, upsert=True)
