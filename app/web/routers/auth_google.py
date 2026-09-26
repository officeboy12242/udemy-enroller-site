"""Sign in with Google (OAuth 2.0 / OpenID Connect).

Enabled only when GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET are set. Creates (or
signs in) a site user keyed to the verified Google email. Such users have no
usable password and always sign in via Google.
"""
import secrets
import urllib.parse

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ... import config, security
from ...models import users as users_model
from ..deps import set_session_cookie

router = APIRouter()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_STATE_COOKIE = "g_oauth_state"


def _redirect_uri(request: Request) -> str:
    base = config.PUBLIC_URL or f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"
    return base.rstrip("/") + "/auth/google/callback"


def _fail(msg: str):
    return RedirectResponse("/login?err=" + urllib.parse.quote(msg), status_code=303)


@router.get("/auth/google/login")
def google_login(request: Request):
    if not config.GOOGLE_ENABLED:
        return _fail("Google sign-in isn't configured on this site.")
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    resp = RedirectResponse(AUTH_URL + "?" + params, status_code=303)
    resp.set_cookie(_STATE_COOKIE, security.sign_session({"s": state}),
                    max_age=600, httponly=True, samesite="lax", path="/", secure=config.SESSION_SECURE)
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not config.GOOGLE_ENABLED:
        return _fail("Google sign-in isn't configured on this site.")
    if error or not code:
        return _fail("Google sign-in was cancelled.")

    saved = security.read_session(request.cookies.get(_STATE_COOKIE, ""), max_age=600)
    if not saved or saved.get("s") != state:
        return _fail("Google sign-in expired. Please try again.")

    try:
        tok = requests.post(TOKEN_URL, data={
            "code": code, "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code",
        }, timeout=20).json()
        access_token = tok.get("access_token")
        if not access_token:
            return _fail("Google did not return a token. Try again.")
        ui = requests.get(USERINFO_URL, headers={"Authorization": "Bearer " + access_token}, timeout=20).json()
    except requests.RequestException:
        return _fail("Could not reach Google. Try again shortly.")

    email = (ui.get("email") or "").strip().lower()
    if not email or not ui.get("email_verified"):
        return _fail("Your Google account has no verified email.")

    user = users_model.get_user_by_email(email)
    if user:
        user_id = user["id"]
    else:
        user_id = users_model.create_user(email, secrets.token_urlsafe(32),
                                          is_admin=users_model.count_users() == 0)

    resp = RedirectResponse("/dashboard", status_code=303)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return set_session_cookie(resp, user_id)
