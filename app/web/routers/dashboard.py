"""Dashboard page, stats API, auto-enroll controls, health."""
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import config
from ...models import accounts as accounts_model
from ...models import auto_state as auto_model
from ...models import enrollments as enroll_model
from ...models import prefs as prefs_model
from ...services import enroll_service, stats
from .. import charts
from ..deps import check_csrf, current_user, render, require_user

router = APIRouter()


@router.get("/")
def index(request: Request):
    return RedirectResponse("/dashboard" if current_user(request) else "/login", status_code=303)


@router.get("/dashboard")
def dashboard(request: Request):
    user = require_user(request)
    s = stats.dashboard_stats(user["id"])
    accounts = accounts_model.get_accounts(user["id"])
    overrides = prefs_model.accounts_with_override(user["id"])
    auto = auto_model.get_auto_state(user["id"])
    # Global auto is on, accounts exist, but none has its own auto switch on:
    # the engine would silently enroll nothing. Warn about it on the dashboard.
    auto_needs_account = bool(auto.get("enabled")) and bool(accounts) and \
        not any(a["is_active"] and a["auto_enroll"] for a in accounts)
    return render(request, "dashboard.html", {
        "accounts": accounts,
        "auto_needs_account": auto_needs_account,
        "account_filters": {a["id"]: {"custom": a["id"] in overrides,
                                      "summary": prefs_model.summary(prefs_model.get_prefs(user["id"], a["id"]))}
                            for a in accounts},
        "auto": auto,
        "recent": enroll_model.get_history(user["id"], limit=8),
        "stats": s,
        "interval_min": config.AUTO_ENROLL_INTERVAL // 60,
        "filter_summary": prefs_model.summary(prefs_model.get_prefs(user["id"])),
        "donut_source": charts.donut([(s["source"]["auto"], "ring-auto"), (s["source"]["manual"], "ring-manual")]),
        "donut_accounts": charts.donut([(s["accounts_active"], "ring-active"),
                                        (max(0, s["accounts_total"] - s["accounts_active"]), "ring-inactive")]),
        "gauge_adoption": charts.gauge(s["source"]["adoption_pct"]),
        "sparkline_week": charts.sparkline(s["week_series"]),
    })


@router.get("/api/stats")
def api_stats(request: Request):
    user = require_user(request)
    return stats.dashboard_stats(user["id"])


@router.get("/api/auto-status")
def auto_status(request: Request):
    user = require_user(request)
    state = auto_model.get_auto_state(user["id"])
    batch = enroll_service.get_batch_status(user["id"])
    return {
        "enabled": bool(state.get("enabled")),
        "running": bool(state.get("running")) or batch.get("status") == "running",
        "last_run": state.get("last_run"),
        "next_run": state.get("next_run"),
        "last_result": state.get("last_result"),
        "total_enrolled": state.get("total_enrolled", 0),
        "batch": batch,
    }


@router.get("/api/engine")
def engine(request: Request):
    """Tiny JSON polled by the topbar indicator on every page."""
    user = require_user(request)
    return enroll_service.engine_status(user["id"])


@router.post("/settings/auto-enroll")
async def toggle_auto_enroll(request: Request, csrf_token: str = Form(""), enabled: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    auto_model.set_auto_enabled(user["id"], enabled == "1")
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
