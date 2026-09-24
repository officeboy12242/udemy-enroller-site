"""Free-course feed.

Fetches current 100%-off Udemy course offers, normalizes them to
{title, url, coupon, image, price} dicts and caches the result.
The upstream source is intentionally an internal detail: nothing in this
module's public results names the provider, and no UI/API text exposes it.
"""
import logging
import threading
import time
from typing import Callable

import requests

from . import config

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
    __slots__ = ("title", "url", "coupon", "image", "price", "expires_at")

    def __init__(self, title: str, url: str, coupon: str | None, image: str | None,
                 price: str | None = None, expires_at: str | None = None):
        self.title = title
        self.url = url
        self.coupon = coupon
        self.image = image
        self.price = price
        self.expires_at = expires_at

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "coupon": self.coupon,
            "image": self.image,
            "price": self.price,
            "expires_at": self.expires_at,
        }


def _fetch_upstream(entry: dict, max_pages: int = 3, limit: int = 60) -> list[CourseOffer]:
    offers: list[CourseOffer] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; course-feed/1.0)"})
    for page in range(1, max_pages + 1):
        params = dict(entry["params"])
        params["page"] = page
        try:
            r = session.get(entry["url"], params=params, timeout=15)
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as e:
            log.warning("feed page %s failed: %s", page, e)
            break
        if not items:
            break
        for it in items:
            try:
                sale_price = float(it.get("sale_price", 0) or 0)
            except (TypeError, ValueError):
                sale_price = 0
            url = it.get("url") or ""
            if sale_price != 0 or "udemy.com" not in url:
                continue
            coupon = None
            if "couponCode=" in url:
                coupon = url.split("couponCode=")[1].split("&")[0]
            price = it.get("price") or it.get("original_price") or ""
            offers.append(CourseOffer(
                title=(it.get("name") or "Untitled course").strip(),
                url=url,
                coupon=coupon,
                image=it.get("image") or it.get("image_480x270") or None,
                price=str(price) if price else None,
                expires_at=it.get("sale_end_time") or it.get("expires_at") or None,
            ))
            if len(offers) >= limit:
                return offers
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
        courses = _fetch_upstream(entry, limit=config.ENROLL_BATCH_LIMIT)
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


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["items"] = []
        _cache["fetched_at"] = 0.0
