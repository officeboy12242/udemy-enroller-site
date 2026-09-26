"""Enrollment filters page: choose languages, categories and a minimum rating.

Counts come from the cached feed only (no network on page load). A compact
facet list is embedded so the page can preview matches live as you click,
without a round trip.
"""
import json
import threading
from collections import Counter

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...models import accounts as accounts_model
from ...models import prefs as prefs_model
from ...services import feed
from ..deps import check_csrf, render, require_user

router = APIRouter()
_refreshing = threading.Event()


def _facets() -> dict:
    offers = feed.get_cached_courses()
    langs = Counter((o.language or "Unknown") for o in offers)
    cats = Counter((o.category or "Other") for o in offers)

    lang_names = [n for n, _ in langs.most_common()] + \
        [n for n in prefs_model.COMMON_LANGUAGES if n not in langs]
    cat_names = [n for n in prefs_model.UDEMY_CATEGORIES] + \
        [n for n in cats if n not in prefs_model.UDEMY_CATEGORIES]

    # Compact rows for the live preview: [language_index, category_index, rating].
    li = {n: i for i, n in enumerate(lang_names)}
    ci = {n: i for i, n in enumerate(cat_names)}
    rows = [[li[o.language or "Unknown"], ci[o.category or "Other"], round(float(o.rating or 0), 1)]
            for o in offers]
    age = feed.cache_age_seconds()
    return {
        "languages": [{"name": n, "count": langs.get(n, 0)} for n in lang_names],
        "categories": [{"name": n, "count": cats.get(n, 0)} for n in cat_names],
        # Names come from the upstream feed; escape "</" so no value can close the <script> tag.
        "rows_json": json.dumps({"l": lang_names, "c": cat_names, "r": rows},
                                separators=(",", ":")).replace("</", "<\\/"),
        "total": len(offers),
        "age_min": None if age is None else int(age // 60),
    }


def _owned_account(user_id: int, account_id: int) -> dict | None:
    if not account_id:
        return None
    return next((a for a in accounts_model.get_accounts(user_id) if a["id"] == account_id), None)


@router.get("/filters")
def filters_page(request: Request, saved: str = "", refreshing: str = "", account: int = 0):
    user = require_user(request)
    accounts = accounts_model.get_accounts(user["id"])
    scope_acc = _owned_account(user["id"], account)
    prefs = prefs_model.get_prefs(user["id"], scope_acc["id"] if scope_acc else None)
    facets = _facets()
    matching = len(prefs_model.filter_offers(feed.get_cached_courses(), prefs)[0])
    overrides = prefs_model.accounts_with_override(user["id"])
    return render(request, "filters.html", {
        "prefs": prefs, "facets": facets, "matching": matching,
        "summary": prefs_model.summary(prefs), "rating_steps": prefs_model.RATING_STEPS,
        "saved": saved == "1",
        "refreshing": refreshing == "1" or _refreshing.is_set(),
        "accounts": accounts, "scope_acc": scope_acc, "overrides": overrides,
        "account_summaries": {a["id"]: prefs_model.summary(prefs_model.get_prefs(user["id"], a["id"]))
                              for a in accounts},
    })


@router.post("/filters")
async def filters_save(request: Request, csrf_token: str = Form(""),
                       languages: list[str] = Form([]), categories: list[str] = Form([]),
                       min_rating: float = Form(0.0), include_unrated: str = Form(""),
                       account_id: int = Form(0)):
    user = require_user(request)
    ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if not check_csrf(csrf_token):
        if ajax:
            return JSONResponse({"ok": False, "error": "Session expired - reload the page."}, status_code=400)
        return RedirectResponse("/filters", status_code=303)
    acc = _owned_account(user["id"], account_id)
    if account_id and not acc:
        return JSONResponse({"ok": False, "error": "Unknown account."}, status_code=404)
    prefs_model.save_prefs(user["id"], languages, categories, min_rating, include_unrated == "1",
                           account_id=acc["id"] if acc else None)
    prefs = prefs_model.get_prefs(user["id"], acc["id"] if acc else None)
    if ajax:
        matching = len(prefs_model.filter_offers(feed.get_cached_courses(), prefs)[0])
        return {"ok": True, "summary": prefs_model.summary(prefs), "matching": matching,
                "scope": "account" if acc else "default"}
    return RedirectResponse(f"/filters?saved=1{'&account=%d' % acc['id'] if acc else ''}", status_code=303)


@router.post("/filters/use-default")
async def filters_use_default(request: Request, csrf_token: str = Form(""), account_id: int = Form(0)):
    user = require_user(request)
    acc = _owned_account(user["id"], account_id)
    if acc and check_csrf(csrf_token):
        prefs_model.clear_account_prefs(user["id"], acc["id"])
    return RedirectResponse(f"/filters?account={account_id}&saved=1", status_code=303)


@router.post("/filters/refresh")
async def filters_refresh(request: Request, csrf_token: str = Form("")):
    """User-initiated feed refresh so counts appear without waiting for the engine."""
    require_user(request)
    if check_csrf(csrf_token) and not _refreshing.is_set():
        # Paging the whole feed can take a while - never block the request/event loop.
        def _run():
            _refreshing.set()
            try:
                feed.get_free_courses(force_refresh=True)
            finally:
                _refreshing.clear()
        threading.Thread(target=_run, daemon=True).start()
    return RedirectResponse("/filters?refreshing=1", status_code=303)
