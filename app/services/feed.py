"""Free-course feed.

Fetches current 100%-off Udemy course offers, normalizes them to
{title, url, coupon, image, price} dicts and caches the result.
The upstream source is intentionally an internal detail: nothing in this
module's public results names the provider, and no UI/API text exposes it.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .. import config

log = logging.getLogger(__name__)

# Internal upstream definition (never surfaced in responses/UI).
_UPSTREAMS: list[dict] = [
    {
        "name": "source_a",
        "url": "https://cdn.real.discount/api/courses",
        "params": {"page": 1, "limit": 50, "sortBy": "sale_start"},
    }
]

_cache_lock = threading.Lock()
_cache: dict = {"items": [], "fetched_at": 0.0, "error": None}


class CourseOffer:
    """Normalized free-course offer."""
    __slots__ = ("title", "url", "coupon", "image", "price", "currency", "expires_at",
                 "language", "category", "subcategory", "rating")

    def __init__(self, title: str, url: str, coupon: str | None, image: str | None,
                 price: str | None = None, currency: str | None = None,
                 expires_at: str | None = None, language: str | None = None,
                 category: str | None = None, subcategory: str | None = None,
                 rating: float = 0.0):
        self.title = title
        self.url = url
        self.coupon = coupon
        self.image = image
        self.price = price
        self.currency = currency
        self.expires_at = expires_at
        self.language = language
        self.category = category
        self.subcategory = subcategory
        self.rating = rating

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "coupon": self.coupon,
            "image": self.image,
            "price": self.price,
            "currency": self.currency,
            "expires_at": self.expires_at,
            "language": self.language,
            "category": self.category,
            "subcategory": self.subcategory,
            "rating": self.rating,
        }


def _parse_item(it: dict) -> CourseOffer | None:
    try:
        sale_price = float(it.get("sale_price", 0) or 0)
    except (TypeError, ValueError):
        sale_price = 0
    url = it.get("url") or ""
    if sale_price != 0 or "udemy.com" not in url:
        return None
    coupon = url.split("couponCode=")[1].split("&")[0] if "couponCode=" in url else None
    price = it.get("price") or it.get("original_price") or ""
    try:
        rating = float(it.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    return CourseOffer(
        title=(it.get("name") or "Untitled course").strip(),
        url=url,
        coupon=coupon,
        image=it.get("image") or it.get("image_480x270") or None,
        price=str(price) if price else None,
        currency=it.get("currency") or it.get("price_currency") or "USD",
        expires_at=it.get("sale_end_time") or it.get("expires_at") or None,
        language=(it.get("language") or "").strip() or None,
        category=(it.get("category") or "").strip() or None,
        subcategory=(it.get("subcategory") or "").strip() or None,
        rating=rating,
    )


def _sale_age_hours(it: dict) -> float | None:
    raw = it.get("sale_start")
    if not raw:
        return None
    try:
        started = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - started).total_seconds() / 3600


def _get_page(session, entry: dict, page: int) -> list | None:
    """One page of listings; retried once. None means the page could not be read."""
    params = dict(entry["params"], page=page)
    for attempt in range(2):
        try:
            r = session.get(entry["url"], params=params, timeout=20)
            r.raise_for_status()
            return r.json().get("items", [])
        except Exception as e:
            log.warning("feed page %s attempt %s failed: %s", page, attempt + 1, e)
    return None


def _fetch_upstream(entry: dict, max_pages: int = 40, parallel: int = 6) -> list[CourseOffer]:
    """Collect every live 100%-off offer the upstream has.

    There is no cap on the number of courses. Paging stops only when the
    upstream runs out, or when listings get older than any coupon can still
    be valid (FEED_MAX_AGE_HOURS) - past that point everything would just be
    rejected as expired. Pages are fetched in parallel batches because the
    upstream is slow (~5-10s per page). max_pages is only a runaway guard.
    """
    offers: list[CourseOffer] = []
    seen: set[str] = set()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; course-feed/1.0)"})
    page = 1
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while page <= max_pages:
            batch = list(range(page, min(page + parallel, max_pages + 1)))
            results = list(pool.map(lambda p: _get_page(session, entry, p), batch))
            done = False
            for items in results:
                if items is None:
                    continue          # unreadable page: skip it, keep the rest
                if not items:
                    done = True       # upstream exhausted
                    break
                ages = [a for a in (_sale_age_hours(it) for it in items) if a is not None]
                for it in items:
                    offer = _parse_item(it)
                    if offer and offer.url not in seen:
                        seen.add(offer.url)
                        offers.append(offer)
                if ages and min(ages) > config.FEED_MAX_AGE_HOURS:
                    done = True       # whole page is older than any live coupon
                    break
            if done or all(r is None for r in results):
                break
            page += parallel
    return offers


def get_free_courses(force_refresh: bool = False) -> dict:
    """Return {'courses': [...], 'fetched_at': iso, 'cached': bool, 'error': str|None}."""
    now = time.time()
    with _cache_lock:
        if not force_refresh and _cache["items"] and now - _cache["fetched_at"] < config.FEED_CACHE_TTL:
            return {
                "courses": list(_cache["items"]),
                "cached": True,
                "error": None,
                "fetched_at": _cache["fetched_at"],
            }
    courses: list[CourseOffer] = []
    err = None
    for entry in _UPSTREAMS:
        courses = _fetch_upstream(entry)
        if courses:
            break
    if not courses:
        err = "Feed temporarily unavailable — will retry automatically."
    with _cache_lock:
        if courses:
            _cache["items"] = courses
            _cache["fetched_at"] = now
            _cache["error"] = None
        elif not _cache["items"]:
            _cache["error"] = err
        return {
            "courses": list(_cache["items"]),
            "cached": False,
            "error": _cache["error"] if not courses else None,
            "fetched_at": _cache["fetched_at"],
        }


def get_cached_courses() -> list[CourseOffer]:
    """Whatever the last successful fetch returned - never touches the network.

    Used by pages that only want to *describe* the feed (filter counts), so a
    page load can't stall on a slow upstream. The engine keeps it warm.
    """
    with _cache_lock:
        return list(_cache["items"])


def cache_age_seconds() -> float | None:
    """Seconds since the last successful fetch, or None if nothing cached yet."""
    with _cache_lock:
        return (time.time() - _cache["fetched_at"]) if _cache["items"] else None


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["items"] = []
        _cache["fetched_at"] = 0.0
