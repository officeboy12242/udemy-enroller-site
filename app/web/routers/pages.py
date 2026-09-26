"""Secondary pages: Activity (history) and My Courses (search inside account)."""
from fastapi import APIRouter, Request

from ...models import accounts as accounts_model
from ...models import enrollments as enroll_model
from ...services import stats
from ...services.udemy_client import UdemyAutoEnroller
from .. import charts
from ..deps import render, require_user

router = APIRouter()


@router.get("/activity")
def activity(request: Request, page: int = 1):
    user = require_user(request)
    per = 50
    rows = enroll_model.get_history(user["id"], limit=per, offset=max(0, page - 1) * per)
    total = enroll_model.count_enrollments(user["id"])
    s = stats.dashboard_stats(user["id"])
    return render(request, "activity.html", {
        "rows": rows, "page": page, "total": total,
        "pages": max(1, (total + per - 1) // per),
        "stats": s,
        "donut_source": charts.donut([(s["source"]["auto"], "ring-auto"), (s["source"]["manual"], "ring-manual")]),
        "sparkline_week": charts.sparkline(s["week_series"]),
    })


@router.get("/my-courses")
def my_courses(request: Request, q: str = "", account_id: int = 0, go: str = ""):
    user = require_user(request)
    results, searched, error = [], False, None
    accounts = accounts_model.get_accounts(user["id"], active_only=True)
    if go:
        # A submit with no query browses the account's full library (newest first).
        searched = True
        targets = [a for a in accounts if (not account_id or a["id"] == account_id)]
        seen = set()
        for acc in targets:
            enroller = UdemyAutoEnroller(access_token=acc["access_token"], client_id=acc.get("client_id") or None)
            res = enroller.search_enrolled_courses(q.strip(), 1, 20)
            if res.get("error"):
                error = res["error"]
            for item in res.get("results", []):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                item["account_name"] = acc.get("udemy_name") or "Account"
                results.append(item)
    return render(request, "my_courses.html", {
        "q": q, "results": results, "searched": searched,
        "error": error, "accounts": accounts, "account_id": account_id,
    })
