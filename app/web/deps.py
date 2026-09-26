"""Shared web helpers: templates, session, auth dependency, CSRF."""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import config, security
from ..models import users as users_model

from .icons import cat_icon

APP_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["cat_icon"] = cat_icon
# Where the "Connect account" buttons should point: the local browser-window
# flow when running on the user's own PC, or the bookmarklet page when hosted.
templates.env.globals["connect_url"] = "/grab-login" if config.LOCAL_BROWSER_LOGIN else "/connect"


class RequireLogin(Exception):
    """Raised by require_user when there is no valid session; handled globally."""


def get_session(request: Request):
    return security.read_session(request.cookies.get(config.SESSION_COOKIE_NAME, ""))


def current_user(request: Request):
    sess = get_session(request)
    if not sess or "uid" not in sess:
        return None
    return users_model.get_user(sess["uid"])


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise RequireLogin()
    return user


def set_session_cookie(resp, user_id: int):
    token = security.sign_session({"uid": user_id})
    resp.set_cookie(
        config.SESSION_COOKIE_NAME, token,
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax",
        path="/", secure=config.SESSION_SECURE,
    )
    return resp


def clear_session_cookie(resp):
    resp.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return resp


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_csrf(token: str) -> bool:
    return security.check_csrf_token(token or "")


def render(request: Request, template: str, context: dict, status_code: int = 200):
    context.setdefault("request", request)
    user = context.setdefault("user", current_user(request))
    context.setdefault("app_name", config.APP_NAME)
    context.setdefault("csrf_token", security.make_csrf_token())
    context.setdefault("year", datetime.now(timezone.utc).year)
    if user and "bell_count" not in context:
        from ..models import enrollments as enroll_model
        today = datetime.now(timezone.utc).date().isoformat()
        context["bell_count"] = enroll_model.count_enrollments(user["id"], since=today)
    return templates.TemplateResponse(request, template, context, status_code=status_code)
