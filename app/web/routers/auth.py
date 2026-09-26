"""Login, register, logout."""
from urllib.parse import unquote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ... import config, security
from ...models import users as users_model
from ..deps import (check_csrf, clear_session_cookie, client_ip, current_user,
                    render, set_session_cookie)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, err: str = ""):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", {"error": unquote(err) if err else None})


@router.post("/login")
async def login_submit(request: Request, email: str = Form(""), password: str = Form(""),
                       csrf_token: str = Form("")):
    if not check_csrf(csrf_token):
        return render(request, "login.html", {"error": "Session expired - please try again."}, 400)

    ip = client_ip(request)
    key = f"login:{ip}:{email.strip().lower()}"
    if not security.login_limiter.hit(key, config.LOGIN_MAX_ATTEMPTS, config.LOGIN_WINDOW_SECONDS):
        return render(request, "login.html",
                      {"error": "Too many attempts. Wait a few minutes and try again."}, 429)

    user = users_model.get_user_by_email(email)
    if not user or not security.verify_password(password, user["password_hash"]):
        return render(request, "login.html", {"error": "Wrong email or password."}, 401)

    security.login_limiter.reset(key)
    resp = RedirectResponse("/dashboard", status_code=303)
    return set_session_cookie(resp, user["id"])


@router.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "register.html", {"error": None})


@router.post("/register")
async def register_submit(request: Request, email: str = Form(""), password: str = Form(""),
                          password2: str = Form(""), csrf_token: str = Form("")):
    if not check_csrf(csrf_token):
        return render(request, "register.html", {"error": "Session expired - please try again."}, 400)

    ip = client_ip(request)
    if not security.general_limiter.hit(f"register:{ip}", 10, 3600):
        return render(request, "register.html", {"error": "Too many attempts. Try later."}, 429)
    if not email or "@" not in email:
        return render(request, "register.html", {"error": "Enter a valid email."}, 400)
    if len(password) < 8:
        return render(request, "register.html", {"error": "Password must be at least 8 characters."}, 400)
    if password != password2:
        return render(request, "register.html", {"error": "Passwords do not match."}, 400)
    if users_model.get_user_by_email(email):
        return render(request, "register.html", {"error": "An account with this email already exists."}, 400)

    uid = users_model.create_user(email, password, is_admin=users_model.count_users() == 0)
    resp = RedirectResponse("/dashboard", status_code=303)
    return set_session_cookie(resp, uid)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    if not check_csrf(csrf_token):
        return RedirectResponse("/", status_code=303)
    resp = RedirectResponse("/login", status_code=303)
    return clear_session_cookie(resp)
