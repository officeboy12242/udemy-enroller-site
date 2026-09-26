"""Connect a Udemy account with a bookmarklet, for when the app is hosted
(e.g. on Render) and cannot open a browser window on the server.

The user signs in to Udemy in their own browser, then clicks a saved bookmark
that posts their Udemy session token, together with a signed connect code that
identifies the site user, to /connect/token. The token is sent in the POST body
only, so it never appears in a URL or an access log.
"""
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import config, security
from ...models import accounts as accounts_model
from ...models import settings as settings_model
from ..deps import check_csrf, client_ip, render, require_user
from ...services.udemy_login import verify_token

router = APIRouter()


def _nonce(user_id: int) -> str:
    """Per-user secret mixed into the connect code so it can be revoked."""
    key = f"connect_nonce:{user_id}"
    val = settings_model.get_setting(key)
    if not val:
        val = secrets.token_urlsafe(8)
        settings_model.set_setting(key, val)
    return val


def _public_origin(request: Request) -> str:
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    return f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"


@router.get("/connect")
def connect_page(request: Request):
    user = require_user(request)
    origin = _public_origin(request)
    code = security.make_connect_code(user["id"], _nonce(user["id"]))
    endpoint = f"{origin}/connect/token"
    # The bookmarklet is built server-side so the code/endpoint are baked in.
    bookmarklet = (
        "javascript:(function(){"
        "var m=document.cookie.match(/(?:^|; )access_token=([^;]+)/);"
        "if(!m){alert('Log in to udemy.com first, then click this on a Udemy tab.');return;}"
        "var c=document.cookie.match(/(?:^|; )client_id=([^;]+)/);"
        "var f=document.createElement('form');f.method='POST';f.action="
        + _js_str(endpoint) + ";f.target='_blank';"
        "function h(n,v){var i=document.createElement('input');i.type='hidden';i.name=n;i.value=v;f.appendChild(i);}"
        "h('t',decodeURIComponent(m[1]));h('c',c?decodeURIComponent(c[1]):'');h('code'," + _js_str(code) + ");"
        "document.body.appendChild(f);f.submit();"
        "})();"
    )
    return render(request, "connect.html", {
        "bookmarklet": bookmarklet, "endpoint": endpoint,
        "hosted": config.ON_RENDER or bool(config.PUBLIC_URL),
        "local_login": config.LOCAL_BROWSER_LOGIN,
    })


@router.post("/connect/rotate")
async def connect_rotate(request: Request, csrf_token: str = Form("")):
    user = require_user(request)
    if check_csrf(csrf_token):
        settings_model.set_setting(f"connect_nonce:{user['id']}", secrets.token_urlsafe(8))
    return RedirectResponse("/connect", status_code=303)


@router.post("/connect/token")
async def connect_token(request: Request, t: str = Form(""), c: str = Form(""), code: str = Form("")):
    """Cross-site target for the bookmarklet. Authenticated by the signed code,
    not the session cookie (which the browser will not send from udemy.com)."""
    ip = client_ip(request)
    if not security.general_limiter.hit(f"connect:{ip}", 30, 3600):
        return _result(request, False, "Too many attempts. Try again later.")

    data = security.read_connect_code(code)
    if not data:
        return _result(request, False, "This connect link is invalid or was reset. Reopen the Connect page and use the fresh bookmark.")
    user_id = data["uid"]
    if data.get("n") != _nonce(user_id):
        return _result(request, False, "This connect link was revoked. Reopen the Connect page for a new bookmark.")

    token = (t or "").strip()
    if len(token) < 20:
        return _result(request, False, "No Udemy login found on that tab. Make sure you're logged in to udemy.com, then click the bookmark there.")

    info = verify_token(token, (c or "").strip())
    if not info.get("valid"):
        return _result(request, False, f"Udemy rejected that login ({info.get('error', 'unknown')}). Log in to udemy.com again and retry.")

    try:
        accounts_model.upsert_account(
            user_id=user_id, access_token=token, client_id=info.get("client_id") or (c or "").strip(),
            udemy_user_id=info.get("udemy_user_id"), udemy_name=info.get("name"),
        )
    except ValueError:
        return _result(request, False, "That Udemy account is already linked to another user here.")
    return _result(request, True, f"Connected {info.get('name') or 'your Udemy account'}. You can close this tab.")


def _result(request: Request, ok: bool, message: str):
    return render(request, "connect_result.html", {"ok": ok, "message": message}, status_code=200 if ok else 400)


def _js_str(s: str) -> str:
    """Safely embed a Python string as a JS string literal inside the bookmarklet."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
