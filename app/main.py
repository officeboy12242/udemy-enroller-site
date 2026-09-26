"""Udemy Enroller - FastAPI application factory.

Routers:
  auth       login / register / logout
  dashboard  dashboard, stats API, auto-enroll controls, health
  accounts   connect Udemy (login-window grab), toggle, remove
  enroll     Enroll Now batch + live progress
  pages      activity (history), my courses

A background engine auto-enrolls linked accounts on an interval, even when the
site is closed.
"""
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .db import get_db
from .services import enroll_service
from .web.deps import RequireLogin
from .web.routers import (accounts, auth, auth_google, connect, dashboard,
                          enroll, filters, pages)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

APP_DIR = Path(__file__).resolve().parent

_engine_stop = threading.Event()


def _engine_loop():
    log.info("Auto-enroll engine started (interval=%ss)", config.AUTO_ENROLL_INTERVAL)
    _engine_stop.wait(10)
    while not _engine_stop.is_set():
        try:
            enrolled = enroll_service.run_auto_enroll_all_users()
            if enrolled:
                log.info("Auto-enroll tick: enrolled %s course(s)", enrolled)
        except Exception:
            log.exception("Auto-enroll tick crashed")
        _engine_stop.wait(config.AUTO_ENROLL_INTERVAL)


def _keepalive_loop():
    """Ping our own /healthz so a free hosting instance never idles out and
    stops the auto-enroll engine. Disabled unless a public URL is configured."""
    import urllib.request
    url = f"{config.PUBLIC_URL}/healthz"
    log.info("Keep-alive pinging %s every %ss", url, config.KEEPALIVE_INTERVAL)
    _engine_stop.wait(config.KEEPALIVE_INTERVAL)
    while not _engine_stop.is_set():
        try:
            urllib.request.urlopen(url, timeout=15).read()
        except Exception as e:
            log.warning("keep-alive ping failed: %s", e)
        _engine_stop.wait(config.KEEPALIVE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()  # connect to MongoDB + ensure indexes before serving
    threading.Thread(target=_engine_loop, name="auto-enroll-engine", daemon=True).start()
    if config.KEEPALIVE_INTERVAL and config.PUBLIC_URL:
        threading.Thread(target=_keepalive_loop, name="keepalive", daemon=True).start()
    yield
    _engine_stop.set()
    log.info("Auto-enroll engine stopped")


app = FastAPI(title=".", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if "text/html" in resp.headers.get("content-type", ""):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.exception_handler(RequireLogin)
async def _require_login_handler(request: Request, exc: RequireLogin):
    return RedirectResponse("/login", status_code=303)


for r in (auth.router, auth_google.router, dashboard.router, accounts.router, connect.router,
          enroll.router, filters.router, pages.router):
    app.include_router(r)
