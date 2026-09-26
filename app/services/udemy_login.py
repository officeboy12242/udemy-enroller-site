"""Udemy email+password login — captures access_token / client_id cookies automatically.

Flow:
  1. GET  https://www.udemy.com/join/login-popup/        -> csrftoken cookie
  2. POST https://www.udemy.com/api-2.0/auth/udemy-auth/login/  (form + XHR headers)
  3. On success the session now holds access_token + client_id cookies.
  4. Verify via /api-2.0/contexts/me/ and pull display name + user id.

Any failure (bad credentials, captcha, 2FA) returns a structured error so the UI
can offer the manual token-paste fallback.
"""
import logging
import re

import requests

log = logging.getLogger(__name__)

LOGIN_PAGE = "https://www.udemy.com/join/login-popup/"
LOGIN_API = "https://www.udemy.com/api-2.0/auth/udemy-auth/login/"
ME_CONTEXT = "https://www.udemy.com/api-2.0/contexts/me/?me=True"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class UdemyLoginError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason          # one of: invalid_credentials, captcha, network, unknown
        self.detail = detail


def login_with_password(email: str, password: str) -> dict:
    """Log in to Udemy. Returns {access_token, client_id, udemy_user_id, name}.

    Raises UdemyLoginError on any failure.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    # 1) Bootstrap: grab csrftoken + affiliate/visitor cookies
    try:
        r = s.get(LOGIN_PAGE, timeout=20)
    except requests.RequestException as e:
        raise UdemyLoginError("network", f"Could not reach udemy.com: {e}")
    if r.status_code != 200:
        raise UdemyLoginError("network", f"udemy.com returned HTTP {r.status_code}")

    csrf = s.cookies.get("csrftoken", domain=".udemy.com") or s.cookies.get("csrftoken") or ""
    if not csrf:
        # Fall back to scraping the csrfmiddlewaretoken from the form HTML
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""
    if not csrf:
        raise UdemyLoginError("unknown", "Could not obtain Udemy CSRF token (blocked?).")

    # 2) Submit credentials
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": LOGIN_PAGE,
        "Origin": "https://www.udemy.com",
        "X-CSRFToken": csrf,
    }
    form = {
        "email": email,
        "password": password,
        "csrfmiddlewaretoken": csrf,
    }
    try:
        r = s.post(LOGIN_API, data=form, headers=headers, timeout=25, allow_redirects=False)
    except requests.RequestException as e:
        raise UdemyLoginError("network", f"Login request failed: {e}")

    if r.status_code in (401, 400):
        # Udemy returns {"detail": [...]} or error strings for bad credentials
        detail = ""
        try:
            data = r.json()
            if isinstance(data, dict):
                comps = data.get("detail") or data.get("error") or data
                if isinstance(comps, (list, tuple)):
                    detail = "; ".join(str(x) for x in comps)
                else:
                    detail = str(comps)
        except Exception:
            detail = (r.text or "")[:200]
        lowered = detail.lower()
        if "captcha" in lowered or "human" in lowered or "challenge" in lowered:
            raise UdemyLoginError("captcha", detail or "Udemy requires a captcha for this login.")
        raise UdemyLoginError("invalid_credentials", detail or "Udemy rejected the email/password.")

    if r.status_code == 403:
        raise UdemyLoginError("captcha", "Udemy blocked this login (403). Try the manual token instead.")

    if r.status_code not in (200, 201, 302):
        raise UdemyLoginError("unknown", f"Unexpected HTTP {r.status_code} from Udemy login.")

    # 3) Success → cookies should now include access_token / client_id
    jar = s.cookies
    access_token = jar.get("access_token", domain=".udemy.com") or jar.get("access_token") or ""
    client_id = jar.get("client_id", domain=".udemy.com") or jar.get("client_id") or ""
    if not access_token:
        raise UdemyLoginError(
            "unknown",
            "Login succeeded but no access_token cookie was issued (2FA or account check required). "
            "Use the manual token option.",
        )

    info = verify_token(access_token, client_id)
    return {
        "access_token": access_token,
        "client_id": client_id or info.get("client_id", ""),
        "udemy_user_id": info.get("udemy_user_id"),
        "name": info.get("name"),
    }


def _parse_me_context(data: dict, client_id: str) -> dict | None:
    """Pull user info out of a /contexts/me response, or None if not logged in."""
    header = data.get("header", {}) or {}
    if not header.get("isLoggedIn"):
        return None
    client = data.get("client", {}) or {}
    users = data.get("users", []) or []
    uid = users[0].get("id") if users else None
    name = (users[0].get("display_name") if users else None) or header.get("displayName") or ""
    return {
        "valid": True,
        "udemy_user_id": uid,
        "name": name.strip() if name else None,
        "client_id": client_id or client.get("clientId", "") or "",
    }


def verify_token(access_token: str, client_id: str = "") -> dict:
    """Verify an access_token; returns {valid, udemy_user_id, name, client_id}.

    Udemy is inconsistent about how it accepts a raw token, so we try several
    ways before giving up:
      1. access_token (+ client_id, if supplied) as cookies -> /contexts/me
      2. Authorization: Bearer <token> header -> /contexts/me
      3. Bearer header -> /api-2.0/users/me/  (last-resort identity check)
    """
    access_token = (access_token or "").strip()
    client_id = (client_id or "").strip()
    if not access_token:
        return {"valid": False, "error": "empty token"}

    last_error = "not logged in"

    def _session(with_cookies: bool, with_bearer: bool) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": "https://www.udemy.com/",
        })
        if with_bearer:
            s.headers["Authorization"] = f"Bearer {access_token}"
        if with_cookies:
            s.cookies.set("access_token", access_token, domain=".udemy.com")
            if client_id:
                s.cookies.set("client_id", client_id, domain=".udemy.com")
        return s

    # Attempts 1 & 2 — /contexts/me with cookie auth, then Bearer auth.
    for with_bearer in (False, True):
        s = _session(with_cookies=True, with_bearer=with_bearer)
        try:
            r = s.get(ME_CONTEXT, timeout=20)
        except requests.RequestException as e:
            last_error = f"network: {e}"
            continue
        if r.status_code != 200:
            last_error = f"HTTP {r.status_code}"
            continue
        try:
            data = r.json()
        except Exception:
            last_error = "bad JSON from Udemy"
            continue
        info = _parse_me_context(data, client_id)
        if info:
            return info
        last_error = "not logged in"

    # Attempt 3 — /users/me returns the user object directly when authed.
    s = _session(with_cookies=True, with_bearer=True)
    try:
        r = s.get("https://www.udemy.com/api-2.0/users/me/", timeout=20)
        if r.status_code == 200:
            u = r.json()
            uid = u.get("id")
            if uid:
                name = (u.get("display_name") or u.get("name") or "").strip() or None
                return {"valid": True, "udemy_user_id": uid, "name": name, "client_id": client_id}
        elif r.status_code in (401, 403):
            last_error = "not logged in"
        else:
            last_error = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        last_error = f"network: {e}"
    except Exception:
        pass

    return {"valid": False, "error": last_error}
