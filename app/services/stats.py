"""Dashboard aggregates and chart series computed from the enrollment log (MongoDB)."""
from datetime import datetime, timedelta, timezone

from ..db import get_db

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
    "AUD": "A$", "CAD": "C$", "JPY": "¥", "BRL": "R$",
}


def currency_symbol(code: str | None) -> str:
    return _CURRENCY_SYMBOLS.get((code or "").upper(), (code or "") + " ")


def _next_milestone(value: float, step: int) -> dict:
    nxt = (int(value) // step + 1) * step
    prev = nxt - step
    pct = round(((value - prev) / step) * 100)
    return {"next": nxt, "pct": max(0, min(100, pct))}


def dashboard_stats(user_id: int) -> dict:
    db = get_db()
    log = db.enrollment_log
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")

    total = log.count_documents({"user_id": user_id})
    today_count = log.count_documents({"user_id": user_id, "created_at": {"$gte": today}})
    week_count = log.count_documents({"user_id": user_id, "created_at": {"$gte": week_ago}})

    # Money saved per currency (dominant bucket reported).
    saved_rows = list(log.aggregate([
        {"$match": {"user_id": user_id, "original_price": {"$gt": 0}}},
        {"$group": {"_id": "$currency", "amt": {"$sum": "$original_price"}, "n": {"$sum": 1}}},
        {"$sort": {"amt": -1}},
    ]))
    if saved_rows:
        saved_amount = round(sum(r["amt"] or 0 for r in saved_rows), 2)
        saved_currency = saved_rows[0]["_id"] or "USD"
        priced_count = sum(r["n"] for r in saved_rows)
    else:
        saved_amount, saved_currency, priced_count = 0.0, "USD", 0

    # 14-day series (group by the date part of the ISO timestamp).
    since = (now - timedelta(days=13)).date()
    day_rows = log.aggregate([
        {"$match": {"user_id": user_id, "created_at": {"$gte": since.isoformat()}}},
        {"$group": {"_id": {"$substrCP": ["$created_at", 0, 10]}, "c": {"$sum": 1}}},
    ])
    by_day = {r["_id"]: r["c"] for r in day_rows}
    day_series = [{"date": (since + timedelta(days=i)).isoformat(),
                   "count": by_day.get((since + timedelta(days=i)).isoformat(), 0)} for i in range(14)]

    # Last-7-day sparkline (its own window; day range differs from the 14-day map).
    week_since = (now - timedelta(days=6)).date()
    wk = {r["_id"]: r["c"] for r in log.aggregate([
        {"$match": {"user_id": user_id, "created_at": {"$gte": week_since.isoformat()}}},
        {"$group": {"_id": {"$substrCP": ["$created_at", 0, 10]}, "c": {"$sum": 1}}},
    ])}
    week_series = [wk.get((week_since + timedelta(days=i)).isoformat(), 0) for i in range(7)]

    # Accounts summary + cached Udemy library size.
    acc_rows = list(db.accounts.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "c": {"$sum": 1}, "a": {"$sum": "$is_active"},
                    "lib": {"$sum": {"$ifNull": ["$total_courses", 0]}},
                    "have_lib": {"$sum": {"$cond": [{"$gt": ["$total_courses", None]}, 1, 0]}},
                    "synced": {"$max": "$total_courses_updated_at"}}},
    ]))
    a = acc_rows[0] if acc_rows else {}
    accounts_total = a.get("c", 0) or 0
    accounts_active = a.get("a", 0) or 0
    library_total = a.get("lib") if a.get("have_lib") else None
    library_synced_at = a.get("synced")

    # Source split.
    src = {r["_id"]: r["c"] for r in log.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": {"$cond": [{"$eq": ["$source", "auto"]}, "auto", "manual"]}, "c": {"$sum": 1}}},
    ])}
    auto_n, manual_n = src.get("auto", 0), src.get("manual", 0)
    adoption_pct = round((auto_n / total) * 100) if total else 0

    def _breakdown(field, limit):
        rows = log.aggregate([
            {"$match": {"user_id": user_id, field: {"$nin": [None, ""]}}},
            {"$group": {"_id": "$" + field, "c": {"$sum": 1}}},
            {"$sort": {"c": -1}}, {"$limit": limit},
        ])
        return [{"name": r["_id"], "count": r["c"]} for r in rows]

    avg_saved_per_course = round(saved_amount / priced_count, 2) if priced_count else 0.0

    return {
        "total_enrolled": total,
        "today_enrolled": today_count,
        "week_enrolled": week_count,
        "money_saved": {
            "amount": saved_amount, "currency": saved_currency, "symbol": currency_symbol(saved_currency),
            "priced_count": priced_count, "untracked": max(0, total - priced_count),
            "avg_per_course": avg_saved_per_course,
        },
        "accounts_total": accounts_total,
        "accounts_active": accounts_active,
        "library": {"total": library_total, "synced_at": library_synced_at},
        "day_series": day_series,
        "week_series": week_series,
        "top_categories": _breakdown("category", 6),
        "top_languages": _breakdown("language", 5),
        "avg_per_day_week": round(week_count / 7, 1),
        "source": {"auto": auto_n, "manual": manual_n, "adoption_pct": adoption_pct},
        "milestones": {"courses": _next_milestone(total, 25), "savings": _next_milestone(saved_amount, 100)},
    }
