"""Connect / manage linked Udemy accounts (login-window grab flow)."""
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import config, security
from ...models import accounts as accounts_model
from ...models import users as users_model
from ...db import get_db
from ...services import browser_grab, cookie_reader
from ..deps import check_csrf, current_user, render, require_user

router = APIRouter()


@router.get("/grab-login")
def grab_login_start(request: Request, close_browsers: str = ""):
    """One-click connect: first silently read the existing Udemy login from the
    user's browsers (no windows); if that's blocked by a running browser, the
    UI offers to close it — /grab-login?close_browsers=1 is the consented retry
    that force-closes those browsers, then rescans (and falls back to a login
    window if there is still no session)."""
    if close_browsers == "1":
        locked = cookie_reader.get_locked_browsers()
        if "Microsoft Edge" in locked:
            browser_grab.close_edge()
        if "Google Chrome" in locked:
            browser_grab.close_chrome()
    browser_grab.reset()
    if not browser_grab.browser_busy.is_set():
        threading.Thread(target=browser_grab.open_login_window,
                         kwargs={"timeout": 300}, daemon=True).start()
    return RedirectResponse("/grab-wait", status_code=303)


@router.get("/grab-wait")
def grab_wait(request: Request):
    return render(request, "grab_wait.html", {})


@router.get("/api/grab-status")
def grab_status(request: Request):
    res = browser_grab.get_last_result()
    if not (res.get("ok") and res.get("status") == "logged_in"):
        return {"status": res.get("status", "running"), "message": res.get("message", "")}

    udemy_uid = res.get("udemy_user_id")
    udemy_name = res.get("name")
    existing = current_user(request)

    if res.get("status") == "close_browsers":
        return {"status": "close_browsers", "message": res.get("message", ""),
                "locked_browsers": res.get("locked_browsers", [])}

    if existing:
        try:
            accounts_model.upsert_account(
                user_id=existing["id"], access_token=res["access_token"],
                client_id=res.get("client_id") or "", udemy_user_id=udemy_uid, udemy_name=udemy_name,
            )
        except ValueError:
            browser_grab.reset()
            return {"status": "error", "message": "That Udemy account is linked to another site user."}
        browser_grab.reset()
        return JSONResponse({"status": "linked", "name": udemy_name, "source": res.get("source", "")})

    # No site session: sign in / create a site user keyed to this Udemy identity.
    row = get_db().execute(
        "SELECT user_id FROM accounts WHERE udemy_user_id=?", (udemy_uid,)
    ).fetchone() if udemy_uid is not None else None
    if row:
        user_id = row["user_id"]
    else:
        placeholder = f"udemy_{udemy_uid}@users.enroller.local"
        user = users_model.get_user_by_email(placeholder)
        user_id = user["id"] if user else users_model.create_user(placeholder, security.make_csrf_token())
    try:
        accounts_model.upsert_account(
            user_id=user_id, access_token=res["access_token"],
            client_id=res.get("client_id") or "", udemy_user_id=udemy_uid, udemy_name=udemy_name,
        )
    except ValueError:
        pass
    browser_grab.reset()
    resp = JSONResponse({"status": "logged_in", "name": udemy_name, "source": res.get("source", "")})
    token = security.sign_session({"uid": user_id})
    resp.set_cookie(config.SESSION_COOKIE_NAME, token, max_age=config.SESSION_MAX_AGE,
                    httponly=True, samesite="lax", path="/", secure=config.SESSION_SECURE)
    return resp


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, request: Request, user=None, csrf_token: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    accounts_model.delete_account(account_id, user["id"])
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/accounts/{account_id}/auto-toggle")
async def account_auto_toggle(account_id: int, request: Request, csrf_token: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    accounts = {a["id"]: a for a in accounts_model.get_accounts(user["id"])}
    acc = accounts.get(account_id)
    if acc:
        accounts_model.set_account_flags(account_id, auto_enroll=not acc["auto_enroll"])
    return RedirectResponse("/dashboard", status_code=303)
