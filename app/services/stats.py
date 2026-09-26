"""Dashboard aggregates and chart series computed from the enrollment log."""
from datetime import datetime, timedelta, timezone

from ..db import get_db

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
    "AUD": "A$", "CAD": "C$", "JPY": "¥", "BRL": "R$",
}


def currency_symbol(code: str | None) -> str:
    return _CURRENCY_SYMBOLS.get((code or "").upper(), (code or "") + " ")


def _iso_date(dt: datetime) -> str:
    return dt.date().isoformat()


def dashboard_stats(user_id: int) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    today = _iso_date(now)
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")

    total = db.execute(
        "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=?", (user_id,)
    ).fetchone()["c"]
    today_count = db.execute(
        "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=? AND date(created_at)=?",
        (user_id, today),
    ).fetchone()["c"]
    week_count = db.execute(
        "SELECT COUNT(*) c FROM enrollment_log WHERE user_id=? AND created_at>=?",
        (user_id, week_ago),
    ).fetchone()["c"]

    # Money saved: sum original prices per currency, report the dominant bucket.
    saved_rows = db.execute(
        """SELECT currency, SUM(original_price) amt, COUNT(*) n
           FROM enrollment_log
           WHERE user_id=? AND original_price IS NOT NULL AND original_price>0
           GROUP BY currency ORDER BY amt DESC""",
        (user_id,),
    ).fetchall()
    if saved_rows:
        top = saved_rows[0]
        saved_amount = round(sum(r["amt"] or 0 for r in saved_rows), 2)
        saved_currency = top["currency"] or "USD"
        priced_count = sum(r["n"] for r in saved_rows)
    else:
        saved_amount, saved_currency, priced_count = 0.0, "USD", 0

    # 14-day enrollment series (fill gaps with zero).
    since = (now - timedelta(days=13)).date()
    rows = db.execute(
        """SELECT date(created_at) d, COUNT(*) c
           FROM enrollment_log
           WHERE user_id=? AND date(created_at)>=?
           GROUP BY d""",
        (user_id, since.isoformat()),
    ).fetchall()
    by_day = {r["d"]: r["c"] for r in rows}
    day_series = []
    for i in range(14):
        d = (since + timedelta(days=i)).isoformat()
        day_series.append({"date": d, "count": by_day.get(d, 0)})

    accts = db.execute(
        """SELECT COUNT(*) c, SUM(is_active) a, SUM(total_courses) lib,
                  MAX(total_courses_updated_at) synced_at
           FROM accounts WHERE user_id=?""",
        (user_id,),
    ).fetchone()
    accounts_total = accts["c"] or 0
    accounts_active = accts["a"] or 0
    library_total = accts["lib"]  # None until at least one run has synced it
    library_synced_at = accts["synced_at"]

    # Source breakdown (auto vs manual) - powers the donut + adoption gauge.
    src_rows = db.execute(
        """SELECT CASE WHEN source='auto' THEN 'auto' ELSE 'manual' END bucket, COUNT(*) c
           FROM enrollment_log WHERE user_id=? GROUP BY bucket""",
        (user_id,),
    ).fetchall()
    auto_n = next((r["c"] for r in src_rows if r["bucket"] == "auto"), 0)
    manual_n = next((r["c"] for r in src_rows if r["bucket"] == "manual"), 0)
    adoption_pct = round((auto_n / total) * 100) if total else 0

    # Last-7-day sparkline (separate from the 14-day area chart; used in a tile).
    week_since = (now - timedelta(days=6)).date()
    wk_rows = db.execute(
        """SELECT date(created_at) d, COUNT(*) c FROM enrollment_log
           WHERE user_id=? AND date(created_at)>=? GROUP BY d""",
        (user_id, week_since.isoformat()),
    ).fetchall()
    wk_by_day = {r["d"]: r["c"] for r in wk_rows}
    week_series = [wk_by_day.get((week_since + timedelta(days=i)).isoformat(), 0) for i in range(7)]

    def _breakdown(column: str, limit: int) -> list[dict]:
        rows = db.execute(
            f"""SELECT {column} name, COUNT(*) c FROM enrollment_log
                WHERE user_id=? AND {column} IS NOT NULL AND {column}<>''
                GROUP BY {column} ORDER BY c DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [{"name": r["name"], "count": r["c"]} for r in rows]

    top_categories = _breakdown("category", 6)
    top_languages = _breakdown("language", 5)

    avg_saved_per_course = round(saved_amount / priced_count, 2) if priced_count else 0.0
    avg_per_day_week = round(week_count / 7, 1)

    def _next_milestone(value: float, step: int) -> dict:
        """Smallest multiple of step strictly greater than value, plus progress toward it."""
        nxt = (int(value) // step + 1) * step
        prev = nxt - step
        pct = round(((value - prev) / step) * 100)
        return {"next": nxt, "pct": max(0, min(100, pct))}

    return {
        "total_enrolled": total,
        "today_enrolled": today_count,
        "week_enrolled": week_count,
        "money_saved": {
            "amount": saved_amount,
            "currency": saved_currency,
            "symbol": currency_symbol(saved_currency),
            "priced_count": priced_count,
            "untracked": max(0, total - priced_count),
            "avg_per_course": avg_saved_per_course,
        },
        "accounts_total": accounts_total,
        "accounts_active": accounts_active,
        "library": {
            "total": library_total,          # None = not synced yet, else real Udemy course count
            "synced_at": library_synced_at,
        },
        "day_series": day_series,
        "week_series": week_series,
        "top_categories": top_categories,
        "top_languages": top_languages,
        "avg_per_day_week": avg_per_day_week,
        "source": {"auto": auto_n, "manual": manual_n, "adoption_pct": adoption_pct},
        "milestones": {
            "courses": _next_milestone(total, 25),
            "savings": _next_milestone(saved_amount, 100),
        },
    }
