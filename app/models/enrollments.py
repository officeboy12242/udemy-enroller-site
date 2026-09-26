"""Enrollment log (one row per course enrolled per account). MongoDB-backed."""
from pymongo.errors import DuplicateKeyError

from ..db import get_db, next_id, now_iso


def log_enrollment(user_id: int, account_id: int, course_id, slug, title, course_url,
                   source: str, image: str | None = None,
                   original_price: float | None = None, currency: str | None = None,
                   category: str | None = None, language: str | None = None) -> bool:
    """Insert enrollment; returns False if this slug was already logged for the account."""
    try:
        get_db().enrollment_log.insert_one({
            "_id": next_id("enrollment_log"),
            "user_id": user_id, "account_id": account_id,
            "udemy_course_id": str(course_id or ""), "slug": slug or "", "title": title or "",
            "course_url": course_url or "", "image": image or "",
            "original_price": original_price, "currency": currency,
            "category": category, "language": language,
            "source": source, "created_at": now_iso(),
        })
        return True
    except DuplicateKeyError:
        return False


def is_enrolled_by_slug(account_id: int, slug: str) -> bool:
    return get_db().enrollment_log.find_one({"account_id": account_id, "slug": slug}) is not None


def get_enrolled_slugs(account_id: int) -> set[str]:
    cur = get_db().enrollment_log.find({"account_id": account_id, "slug": {"$nin": ["", None]}}, {"slug": 1})
    return {d["slug"] for d in cur if d.get("slug")}


def get_history(user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    cur = (get_db().enrollment_log.find({"user_id": user_id})
           .sort([("created_at", -1), ("_id", -1)]).skip(max(0, offset)).limit(limit))
    out = []
    for d in cur:
        d = dict(d); d["id"] = d.pop("_id"); out.append(d)
    return out


def count_enrollments(user_id: int, since: str | None = None) -> int:
    q = {"user_id": user_id}
    if since:
        q["created_at"] = {"$gte": since}
    return get_db().enrollment_log.count_documents(q)
