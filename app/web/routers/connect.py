"""Connect a Udemy account by pasting its session token.

Udemy guards every login with Cloudflare Turnstile, so the sign-in must happen
in the user's own browser and the app can't log in server-side. Without a
bookmarklet or extension, the remaining zero-install option is to paste the
`access_token` cookie. This page guides the user to it and validates instantly.
The token is submitted same-origin (session + CSRF protected) and stored
encrypted; it is never placed in a URL.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from ... import config
from ...models import accounts as accounts_model
from ..deps import check_csrf, render, require_user
from ...services.udemy_login import verify_token

router = APIRouter()


@router.get("/connect")
def connect_page(request: Request):
    require_user(request)
    return render(request, "connect.html", {"local_login": config.LOCAL_BROWSER_LOGIN})


@router.post("/connect/paste")
async def connect_paste(request: Request, csrf_token: str = Form(""),
                        access_token: str = Form(""), client_id: str = Form("")):
    user = require_user(request)
    if not check_csrf(csrf_token):
        return JSONResponse({"ok": False, "error": "Session expired - reload the page."}, status_code=400)

    token = (access_token or "").strip().strip('"')
    if len(token) < 20:
        return JSONResponse({"ok": False, "error": "That doesn't look like a full access_token. "
                             "Copy the whole value."}, status_code=400)

    info = verify_token(token, (client_id or "").strip())
    if not info.get("valid"):
        return JSONResponse({"ok": False, "error": f"Udemy didn't accept that token ({info.get('error', 'invalid')}). "
                             "Make sure you're logged in to udemy.com and copy a fresh access_token."},
                            status_code=400)

    try:
        accounts_model.upsert_account(
            user_id=user["id"], access_token=token,
            client_id=info.get("client_id") or (client_id or "").strip(),
            udemy_user_id=info.get("udemy_user_id"), udemy_name=info.get("name"),
        )
    except ValueError:
        return JSONResponse({"ok": False, "error": "That Udemy account is already linked to another user here."},
                            status_code=409)
    return {"ok": True, "name": info.get("name") or "your Udemy account"}
