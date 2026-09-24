"""Udemy Enroller site — FastAPI application.

Routes (all server-rendered, API docs disabled):
  /                -> redirect to dashboard or login
  /login /register /logout
  /dashboard       -> auto-enroll command center
  /courses         -> free-course feed + Enroll / Enroll All
  /history         -> enrollment log
  /my-courses      -> search inside linked Udemy accounts
  /healthz         -> liveness probe

Security: signed HttpOnly session cookies, CSRF on every form, rate-limited
logins, security headers, access code gated registration, encrypted tokens.
"""
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, status as http_status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, enroll_service, security, store
from .feed import get_free_courses
from .udemy_enroller import UdemyAutoEnroller
from .udemy_login import UdemyLoginError, login_with_password, verify_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── Background auto-enroll engine ───────────────────────────────────────────
_engine_stop = threading.Event()
engine_busy = threading.Event()


def _engine_loop():
    log.info("Auto-enroll engine started (interval=%ss)", config.AUTO_ENROLL_INTERVAL)
    # Small delay so the web server binds first
    _engine_stop.wait(10)
    while not _engine_stop.is_set():
        try:
            if not engine_busy.is_set():
                engine_busy.set()
                try:
                    enrolled = enroll_service.run_auto_enroll_all_users()
                    if enrolled:
                        log.info("Auto-enroll tick: enrolled %s course(s)", enrolled)
                finally:
                    engine_busy.clear()
        except Exception:
            log.exception("Auto-enroll tick crashed")
        _engine_stop.wait(config.AUTO_ENROLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_engine_loop, name="auto-enroll-engine", daemon=True)
    t.start()
    yield
    _engine_stop.set()
    log.info("Auto-enroll engine stopped")


app = FastAPI(
    title=".",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


# ── Session helpers ─────────────────────────────────────────────────────────

def _get_session(request: Request):
    return security.read_session(request.cookies.get(config.SESSION_COOKIE_NAME, ""))


def current_user(request: Request):
    sess = _get_session(request)
    if not sess or "uid" not in sess:
        return None
    return store.get_user(sess["uid"])


def require_user(request: Request):
    user = current_user(request)
    if not user:
        RedirectResponse._raise = True
        raise _RedirectToLogin()
    return user


class _RedirectToLogin(Exception):
    pass


@app.exception_handler(_RedirectToLogin)
async def _redirect_handler(request: Request, exc: _RedirectToLogin):
    return RedirectResponse("/login", status_code=303)


def _set_session_cookie(resp, user_id: int):
    token = security.sign_session({"uid": user_id})
    resp.set_cookie(
        config.SESSION_COOKIE_NAME, token,
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax",
        path="/", secure=False,  # set True behind HTTPS in production
    )
    return resp


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _check_csrf(request: Request, token: str) -> bool:
    return security.check_csrf_token(token or "")


def _render(request: Request, template: str, context: dict, status_code: int = 200):
    user = current_user(request)
    context.setdefault("request", request)
    context.setdefault("user", user)
    context.setdefault("app_name", config.APP_NAME)
    context.setdefault("csrf_token", security.make_csrf_token())
    context.setdefault("year", datetime.now(timezone.utc).year)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


# ── Public pages ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "login.html", {"error": None, "mode": "password"})


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    if not await _check_csrf(request, csrf_token):
        return _render(request, "login.html", {"error": "Session expired — please try again.", "mode": "password"}, 400)

    ip = _client_ip(request)
    key = f"login:{ip}:{email.strip().lower()}"
    if not security.login_limiter.hit(key, config.LOGIN_MAX_ATTEMPTS, config.LOGIN_WINDOW_SECONDS):
        return _render(
            request, "login.html",
            {"error": "Too many attempts. Wait a few minutes and try again.", "mode": "password"}, 429,
        )

    user = store.get_user_by_email(email)
    if not user or not security.verify_password(password, user["password_hash"]):
        return _render(request, "login.html", {"error": "Wrong email or password.", "mode": "password"}, 401)

    security.login_limiter.reset(key)
    resp = RedirectResponse("/dashboard", status_code=303)
    return _set_session_cookie(resp, user["id"])


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "register.html", {"error": None})


@app.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    access_code: str = Form(""),
    csrf_token: str = Form(""),
):
    if not await _check_csrf(request, csrf_token):
        return _render(request, "register.html", {"error": "Session expired — please try again."}, 400)

    ip = _client_ip(request)
    if not security.general_limiter.hit(f"register:{ip}", 10, 3600):
        return _render(request, "register.html", {"error": "Too many attempts. Try later."}, 429)

    if access_code.strip() != config.SITE_ACCESS_CODE:
        return _render(request, "register.html", {"error": "Invalid access code."}, 403)
    if not email or "@" not in email:
        return _render(request, "register.html", {"error": "Enter a valid email."}, 400)
    if len(password) < 8:
        return _render(request, "register.html", {"error": "Password must be at least 8 characters."}, 400)
    if password != password2:
        return _render(request, "register.html", {"error": "Passwords do not match."}, 400)
    if store.get_user_by_email(email):
        return _render(request, "register.html", {"error": "An account with this email already exists."}, 400)

    uid = store.create_user(email, password, is_admin=store.count_users() == 0)
    resp = RedirectResponse("/dashboard", status_code=303)
    return _set_session_cookie(resp, uid)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return RedirectResponse("/", status_code=303)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return resp


# ── Dashboard ───────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(require_user), linked: str = ""):
    accounts = store.get_accounts(user["id"])
    state = store.get_auto_state(user["id"])
    history = store.get_history(user["id"], limit=10)

    flash = None
    if linked == "1":
        flash = ("ok", "Udemy account linked from your browser! Token captured automatically.")
    elif linked.startswith("e:"):
        from urllib.parse import unquote
        flash = ("error", unquote(linked[2:]))

    # Bookmarklet origin follows whatever host the user is browsing
    host = request.headers.get("host", f"127.0.0.1:{config.PORT}")
    origin = f"{request.url.scheme}://{host}"
    bookmarklet = (
        "javascript:(function(){"
        "var m=document.cookie.match(/(?:^|;\\s*)access_token=([^;]+)/);"
        "if(!m){alert('Udemy access_token not visible - make sure you are logged in to udemy.com in this browser. If it still fails, use the token paste option on the site.');return;}"
        "var c=(document.cookie.match(/(?:^|;\\s*)client_id=([^;]+)/)||[])[1]||'';"
        f"location.href='{origin}/grab?t='+encodeURIComponent(m[1])+'&c='+encodeURIComponent(c);"
        "})()"
    )

    return _render(request, "dashboard.html", {
        "accounts": accounts,
        "auto": state,
        "recent": history,
        "flash": flash,
        "bookmarklet": bookmarklet,
        "today_count": store.count_enrollments(user["id"], since=datetime.now(timezone.utc).date().isoformat()),
        "total_count": store.count_enrollments(user["id"]),
        "interval_min": config.AUTO_ENROLL_INTERVAL // 60,
    })


@app.post("/settings/auto-enroll")
async def toggle_auto_enroll(request: Request, user=Depends(require_user), csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    enabled = (await request.form()).get("enabled") == "1"
    store.set_auto_enabled(user["id"], enabled)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/settings/auto-enroll-now")
async def auto_enroll_now(request: Request, user=Depends(require_user), csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    state = store.get_auto_state(user["id"])
    if state.get("running"):
        return RedirectResponse("/dashboard", status_code=303)
    threading.Thread(
        target=enroll_service.run_auto_enroll_for_user_and_record,
        args=(user["id"],), daemon=True,
    ).start()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/auto-status")
async def auto_status(user=Depends(require_user)):
    """Polled by the dashboard for live auto-enroll status."""
    state = store.get_auto_state(user["id"])
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


# ── One-click browser grab (bookmarklet target) ────────────────────────────

@app.get("/grab")
def grab_token(request: Request, user=Depends(require_user), t: str = "", c: str = ""):
    """Receives the token pulled from a logged-in udemy.com tab by the bookmarklet."""
    from urllib.parse import quote
    token = (t or "").strip()
    client_id = (c or "").strip()
    if len(token) < 20:
        return RedirectResponse(
            f"/dashboard?linked=e:{quote('No token received — are you logged in to udemy.com?')}",
            status_code=303,
        )
    info = verify_token(token, client_id)
    if not info.get("valid"):
        return RedirectResponse(
            f"/dashboard?linked=e:{quote('Udemy rejected the token from your browser (' + str(info.get('error')) + '). It may be expired - log in to udemy.com again.')}",
            status_code=303,
        )
    store.upsert_account(
        user_id=user["id"],
        access_token=token,
        client_id=info.get("client_id") or client_id,
        udemy_user_id=info.get("udemy_user_id"),
        udemy_name=info.get("name"),
    )
    return RedirectResponse("/dashboard?linked=1", status_code=303)


# ── Account linking ─────────────────────────────────────────────────────────

@app.post("/accounts/link")
async def link_account(
    request: Request,
    user=Depends(require_user),
    csrf_token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    access_token: str = Form(""),
    client_id: str = Form(""),
    mode: str = Form("password"),
):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "Session expired — reload the page."}, status_code=400)

    try:
        if mode == "token":
            token = access_token.strip()
            if len(token) < 20:
                return JSONResponse({"error": "That token looks too short."}, status_code=400)
            info = verify_token(token, client_id.strip())
            if not info.get("valid"):
                return JSONResponse({"error": f"Udemy rejected the token: {info.get('error', 'unknown')}"}, status_code=400)
            acc_token = token
            cl_id = info.get("client_id") or client_id.strip()
        else:
            if not email or not password:
                return JSONResponse({"error": "Email and password are required."}, status_code=400)
            try:
                creds = login_with_password(email, password)
            except UdemyLoginError as e:
                hint = {
                    "invalid_credentials": "Udemy says the email or password is wrong.",
                    "captcha": "Udemy asked for a captcha. Use the manual token option instead.",
                    "network": "Could not reach udemy.com from the server. Try again shortly.",
                }.get(e.reason, e.detail)
                return JSONResponse({"error": hint, "fallback": "token"}, status_code=401)
            acc_token = creds["access_token"]
            cl_id = creds["client_id"]
            info = {"udemy_user_id": creds["udemy_user_id"], "name": creds["name"]}

        account_id = store.upsert_account(
            user_id=user["id"],
            access_token=acc_token,
            client_id=cl_id,
            udemy_user_id=info.get("udemy_user_id"),
            udemy_name=info.get("name"),
        )
        return {"ok": True, "account_id": account_id, "name": info.get("name")}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        log.exception("link_account failed")
        return JSONResponse({"error": f"Unexpected error: {e}"}, status_code=500)


@app.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, request: Request, user=Depends(require_user), csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    store.delete_account(account_id, user["id"])
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/accounts/{account_id}/auto-toggle")
async def account_auto_toggle(account_id: int, request: Request, user=Depends(require_user), csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF failed"}, status_code=400)
    accounts = {a["id"]: a for a in store.get_accounts(user["id"])}
    acc = accounts.get(account_id)
    if acc:
        store.set_account_flags(account_id, auto_enroll=not acc["auto_enroll"])
    return RedirectResponse("/dashboard", status_code=303)


# ── Courses (feed) ──────────────────────────────────────────────────────────

@app.get("/courses", response_class=HTMLResponse)
def courses_page(request: Request, user=Depends(require_user), refresh: str = ""):
    force = refresh == "1"
    feed = get_free_courses(force_refresh=force)
    enrolled_slugs = set()
    accounts = store.get_accounts(user["id"], active_only=True)
    for acc in accounts:
        enrolled_slugs |= store.get_enrolled_slugs(acc["id"])
    courses = []
    for c in feed["courses"]:
        d = c.to_dict()
        slug = (c.url.rstrip("/").split("/") or [""])[-1]
        d["slug"] = slug
        d["enrolled"] = slug in enrolled_slugs
        courses.append(d)
    return _render(request, "courses.html", {
        "courses": courses,
        "feed_error": feed["error"],
        "cached": feed["cached"],
        "has_account": bool(accounts),
    })


@app.post("/enroll")
async def enroll_one(
    request: Request,
    user=Depends(require_user),
    csrf_token: str = Form(""),
    slug: str = Form(""),
    title: str = Form(""),
    image: str = Form(""),
    coupon: str = Form(""),
):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "Session expired — reload the page."}, status_code=400)
    accounts = store.get_accounts(user["id"], active_only=True)
    if not accounts:
        return JSONResponse({"error": "Link a Udemy account first."}, status_code=400)
    url = f"https://www.udemy.com/course/{slug}/"
    if coupon:
        url += f"?couponCode={coupon}"

    class _Offer:
        pass

    offer = _Offer()
    offer.title = title
    offer.url = url
    offer.coupon = coupon or None
    offer.image = image or None

    account = accounts[0]
    result = enroll_service.enroll_single(account, offer)
    return result


@app.post("/enroll-all")
async def enroll_all(request: Request, user=Depends(require_user), csrf_token: str = Form("")):
    if not await _check_csrf(request, csrf_token):
        return JSONResponse({"error": "Session expired — reload the page."}, status_code=400)
    accounts = store.get_accounts(user["id"], active_only=True)
    if not accounts:
        return JSONResponse({"error": "Link a Udemy account first."}, status_code=400)
    result = enroll_service.start_enroll_all(user["id"])
    return result


@app.get("/api/enroll-all/status")
async def enroll_all_status(user=Depends(require_user)):
    return enroll_service.get_batch_status(user["id"])


# ── History & My courses ────────────────────────────────────────────────────

@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, user=Depends(require_user), page: int = 1):
    per = 50
    rows = store.get_history(user["id"], limit=per, offset=max(0, page - 1) * per)
    total = store.count_enrollments(user["id"])
    return _render(request, "history.html", {
        "rows": rows, "page": page, "total": total,
        "pages": max(1, (total + per - 1) // per),
    })


@app.get("/my-courses", response_class=HTMLResponse)
def my_courses(request: Request, user=Depends(require_user), q: str = "", account_id: int = 0):
    results = []
    searched = False
    error = None
    accounts = store.get_accounts(user["id"], active_only=True)
    if q.strip():
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
    return _render(request, "my_courses.html", {
        "q": q, "results": results, "searched": searched,
        "error": error, "accounts": accounts, "account_id": account_id,
    })


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
