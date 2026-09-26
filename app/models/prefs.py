"""Per-user enrollment filters (languages, categories, minimum rating).

Empty language/category lists mean "any". Shared by manual Enroll Now and the
background auto-enroll engine so both always honour the same rules.
"""
from ..db import get_db, now_iso

# Udemy's own top-level categories, in the order udemy.com lists them.
UDEMY_CATEGORIES = [
    "Development", "Business", "Finance & Accounting", "IT & Software",
    "Office Productivity", "Personal Development", "Design", "Marketing",
    "Lifestyle", "Photography & Video", "Health & Fitness", "Music",
    "Teaching & Academics",
]

COMMON_LANGUAGES = [
    "English", "Spanish", "Portuguese", "French", "German", "Italian", "Japanese",
    "Arabic", "Turkish", "Hindi", "Russian", "Chinese", "Korean", "Indonesian",
    "Polish", "Dutch", "Vietnamese",
]

RATING_STEPS = [0.0, 3.5, 4.0, 4.5]

_DEFAULT = {"languages": [], "categories": [], "min_rating": 0.0, "include_unrated": True}


def _doc_to_prefs(doc, scope: str) -> dict:
    return {
        "languages": [str(v) for v in (doc.get("languages") or []) if v],
        "categories": [str(v) for v in (doc.get("categories") or []) if v],
        "min_rating": float(doc.get("min_rating") or 0),
        "include_unrated": bool(doc.get("include_unrated")),
        "updated_at": doc.get("updated_at"),
        "scope": scope,
    }


def get_prefs(user_id: int, account_id: int | None = None) -> dict:
    """Filters for one account (its own override if set) or the user's default."""
    db = get_db()
    if account_id is not None:
        doc = db.account_prefs.find_one({"_id": account_id, "user_id": user_id})
        if doc:
            return _doc_to_prefs(doc, "account")
    doc = db.enroll_prefs.find_one({"_id": user_id})
    if doc:
        return _doc_to_prefs(doc, "default")
    return dict(_DEFAULT, languages=[], categories=[], updated_at=None, scope="default")


def accounts_with_override(user_id: int) -> set[int]:
    return {d["_id"] for d in get_db().account_prefs.find({"user_id": user_id}, {"_id": 1})}


def save_prefs(user_id: int, languages: list[str], categories: list[str],
               min_rating: float, include_unrated: bool, account_id: int | None = None) -> None:
    min_rating = min(RATING_STEPS, key=lambda s: abs(s - float(min_rating or 0)))
    fields = {
        "user_id": user_id,
        "languages": sorted(set(languages)), "categories": sorted(set(categories)),
        "min_rating": min_rating, "include_unrated": int(include_unrated), "updated_at": now_iso(),
    }
    coll = get_db().enroll_prefs if account_id is None else get_db().account_prefs
    key = user_id if account_id is None else account_id
    coll.update_one({"_id": key}, {"$set": fields}, upsert=True)


def clear_account_prefs(user_id: int, account_id: int) -> None:
    """Make an account follow the default filters again."""
    get_db().account_prefs.delete_one({"_id": account_id, "user_id": user_id})


def is_active(prefs: dict) -> bool:
    return bool(prefs["languages"] or prefs["categories"] or prefs["min_rating"] > 0)


def offer_matches(offer, prefs: dict) -> bool:
    """True if a feed offer passes this user's filters."""
    langs, cats = prefs["languages"], prefs["categories"]
    # Same fallback labels the Filters page shows for missing values.
    if langs and (getattr(offer, "language", None) or "Unknown") not in langs:
        return False
    if cats and (getattr(offer, "category", None) or "Other") not in cats:
        return False
    min_rating = prefs["min_rating"]
    if min_rating > 0:
        rating = float(getattr(offer, "rating", 0) or 0)
        if rating <= 0:
            return prefs["include_unrated"]
        if rating < min_rating:
            return False
    return True


def filter_offers(offers: list, prefs: dict) -> tuple[list, int]:
    """(matching offers, number filtered out)."""
    if not is_active(prefs):
        return list(offers), 0
    kept = [o for o in offers if offer_matches(o, prefs)]
    return kept, len(offers) - len(kept)


def summary(prefs: dict) -> str:
    """Short human label, e.g. 'English, Spanish · Development · 4.0+ rating'."""
    if not is_active(prefs):
        return "All courses"
    parts = []
    if prefs["languages"]:
        parts.append(_short(prefs["languages"]))
    if prefs["categories"]:
        parts.append(_short(prefs["categories"]))
    if prefs["min_rating"] > 0:
        parts.append(f"{prefs['min_rating']:.1f}+ rating")
    return " · ".join(parts)


def _short(items: list[str], keep: int = 2) -> str:
    return ", ".join(items[:keep]) + (f" +{len(items) - keep}" if len(items) > keep else "")
