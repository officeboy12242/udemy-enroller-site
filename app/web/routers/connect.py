"""Connect a Udemy account via the companion browser extension.

Udemy now guards every login with Cloudflare Turnstile, so the sign-in must
happen in the user's own browser. The extension reads the resulting Udemy
session cookies in that browser and POSTs them here, authenticated by a signed
pairing code (not the session cookie, which a cross-site request won't send).
The token travels in the POST body only - never in a URL or access log.
"""
import base64
import io
import json
import secrets
import zipfile
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ... import config, security
from ...models import accounts as accounts_model
from ...models import settings as settings_model
from ..deps import check_csrf, client_ip, render, require_user
from ...services.udemy_login import verify_token

router = APIRouter()

EXTENSION_DIR = config.BASE_DIR / "extension"


def _nonce(user_id: int) -> str:
    """Per-user secret mixed into the pairing code so it can be revoked."""
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


def _pairing_code(origin: str, code: str) -> str:
    raw = json.dumps({"u": origin, "c": code}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@router.get("/connect")
def connect_page(request: Request):
    user = require_user(request)
    origin = _public_origin(request)
    code = security.make_connect_code(user["id"], _nonce(user["id"]))
    return render(request, "connect.html", {
        "pairing": _pairing_code(origin, code),
        "origin_host": origin.split("://")[-1],
        "local_login": config.LOCAL_BROWSER_LOGIN,
    })


@router.get("/connect/extension.zip")
def connect_extension_zip(request: Request):
    require_user(request)
    if not EXTENSION_DIR.is_dir():
        return JSONResponse({"error": "Extension files not found on the server."}, status_code=404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(EXTENSION_DIR.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(EXTENSION_DIR).as_posix())
    buf.seek(0)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="udemy-enroller-extension.zip"'})


@router.post("/connect/rotate")
async def connect_rotate(request: Request, csrf_token: str = Form("")):
    user = require_user(request)
    if check_csrf(csrf_token):
        settings_model.set_setting(f"connect_nonce:{user['id']}", secrets.token_urlsafe(8))
    return RedirectResponse("/connect", status_code=303)


@router.post("/connect/token")
async def connect_token(request: Request, t: str = Form(""), c: str = Form(""), code: str = Form("")):
    """Target for the extension. Authenticated by the signed pairing code."""
    wants_json = "application/json" in request.headers.get("accept", "")

    if not security.general_limiter.hit(f"connect:{client_ip(request)}", 30, 3600):
        return _result(request, wants_json, False, "Too many attempts. Try again later.")

    data = security.read_connect_code(code)
    if not data:
        return _result(request, wants_json, False,
                       "This pairing code is invalid or was reset. Copy a fresh one from the Connect page.")
    user_id = data["uid"]
    if data.get("n") != _nonce(user_id):
        return _result(request, wants_json, False,
                       "This pairing code was revoked. Copy a fresh one from the Connect page.")

    token = (t or "").strip()
    if len(token) < 20:
        return _result(request, wants_json, False,
                       "No Udemy login found in this browser. Log in to udemy.com, then connect.")

    info = verify_token(token, (c or "").strip())
    if not info.get("valid"):
        return _result(request, wants_json, False,
                       f"Udemy rejected that login ({info.get('error', 'unknown')}). Log in to udemy.com again.")

    try:
        accounts_model.upsert_account(
            user_id=user_id, access_token=token, client_id=info.get("client_id") or (c or "").strip(),
            udemy_user_id=info.get("udemy_user_id"), udemy_name=info.get("name"),
        )
    except ValueError:
        return _result(request, wants_json, False, "That Udemy account is already linked to another user here.")
    return _result(request, wants_json, True, "Connected", name=info.get("name") or "your Udemy account")


def _result(request: Request, wants_json: bool, ok: bool, message: str, name: str = ""):
    if wants_json:
        payload = {"ok": ok, "name": name} if ok else {"ok": False, "error": message}
        return JSONResponse(payload, status_code=200 if ok else 400)
    text = f"Connected {name}. You can close this tab." if ok else message
    return render(request, "connect_result.html", {"ok": ok, "message": text},
                  status_code=200 if ok else 400)
