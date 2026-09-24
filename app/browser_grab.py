"""Server-side Udemy login grab — uses the user's REAL browser profile.

Strategy:
  1. Prefer Microsoft Edge with the user's actual "User Data" profile
     (their everyday browser → their existing Udemy login is detected
     immediately, zero typing).
  2. If Edge is running (profile locked), report status "close_edge" so the
     UI can offer a one-click "Close Edge & retry".
  3. If Edge is not installed, fall back to the app's own persistent Chrome
     profile (user logs in inside that window once; it sticks).

Public API:
    grab_from_browser(timeout=180) -> {ok, status, ...}
        status: logged_in | not_logged_in | close_edge | error | busy
    close_edge() -> bool          (taskkill msedge — called from explicit UI button)
    browser_busy : threading.Event
"""
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .udemy_login import verify_token

log = logging.getLogger(__name__)

browser_busy = threading.Event()
_last_result: dict = {"status": "idle"}

UDEMY_URL = "https://www.udemy.com"
EDGE_PORT = 9455       # debugging port when we open the user's real Edge profile
CHROME_PORT = 9444     # debugging port for the app's own chrome profile

_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _edge_paths() -> tuple:
    exe = next((p for p in _EDGE_PATHS if Path(p).exists()), None)
    user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    return exe, str(user_data) if user_data.exists() else None


def _process_running(image: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        return image.lower() in out
    except Exception:
        return False


def close_edge() -> bool:
    """Force-close Microsoft Edge (user consented via UI button)."""
    try:
        subprocess.run(
            ["taskkill", "/IM", "msedge.exe", "/F"],
            capture_output=True, text=True, timeout=15,
        )
        time.sleep(2)
        return not _process_running("msedge.exe")
    except Exception:
        return False


def _read_cookies(page) -> dict:
    cookies = page.cookies(all_domains=True)
    out = {}
    for c in cookies or []:
        name = c.get("name") or ""
        if name in ("access_token", "client_id"):
            out[name] = c.get("value") or ""
    return out


def grab_from_browser(timeout: int = 180) -> dict:
    global _last_result
    if browser_busy.is_set():
        res = {"ok": False, "status": "busy", "message": "A browser session is already running."}
        _last_result = res
        return res
    browser_busy.set()
    try:
        edge_exe, edge_profile = _edge_paths()
        if edge_exe and edge_profile:
            if _process_running("msedge.exe"):
                res = {
                    "ok": False, "status": "close_edge",
                    "message": "Microsoft Edge is running, so its login data is locked. "
                               "Close Edge (or click the button) and grab again.",
                }
                _last_result = res
                return res
            return _grab_with_browser(
                browser_path=edge_exe, user_data=edge_profile,
                port=EDGE_PORT, extra_args=["--profile-directory=Default"],
                timeout=timeout, label="Edge (your profile)",
            )
        # Fallback: app's own persistent Chrome profile
        return _grab_with_browser(
            browser_path=None,
            user_data=str(config.DATA_DIR / "chrome_profile"),
            port=CHROME_PORT, extra_args=[],
            timeout=timeout, label="app Chrome profile",
        )
    finally:
        browser_busy.clear()


def _grab_with_browser(browser_path, user_data, port, extra_args, timeout, label) -> dict:
    global _last_result
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    if browser_path:
        co.set_browser_path(browser_path)
    co.set_local_port(port)
    co.set_user_data_path(user_data)
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--disable-crash-reporter")
    for arg in extra_args:
        co.set_argument(arg)

    page = None
    try:
        page = ChromiumPage(co)
        page.get(UDEMY_URL)
        page.wait.doc_loaded(timeout=40)

        deadline = time.time() + timeout
        token = ""
        closed_early = False
        while time.time() < deadline:
            try:
                cookies = _read_cookies(page)
            except Exception:
                closed_early = True   # user closed the window mid-wait
                break
            token = cookies.get("access_token", "")
            if token:
                break
            time.sleep(2)

        if closed_early:
            _last_result = {
                "ok": False, "status": "not_logged_in",
                "message": "The browser window was closed before a login was detected. "
                           "Click Grab again and leave the window open.",
            }
            return _last_result

        cookies = {}
        try:
            cookies = _read_cookies(page)
        except Exception:
            pass
        token = cookies.get("access_token", "")
        client_id = cookies.get("client_id", "")

        if not token:
            _last_result = {
                "ok": False, "status": "not_logged_in",
                "message": f"No Udemy login detected in {label}. "
                           "Log in to udemy.com inside the window that opened, then grab again.",
            }
            return _last_result

        info = verify_token(token, client_id)
        if not info.get("valid"):
            _last_result = {
                "ok": False, "status": "not_logged_in",
                "message": f"Token found but Udemy rejected it ({info.get('error')}). "
                           "Log in again and retry.",
            }
            return _last_result

        _last_result = {
            "ok": True, "status": "logged_in",
            "access_token": token,
            "client_id": info.get("client_id") or client_id,
            "udemy_user_id": info.get("udemy_user_id"),
            "name": info.get("name"),
        }
        return _last_result
    except Exception as e:
        log.exception("browser grab failed (%s)", label)
        msg = str(e)
        if "connect" in msg.lower() or "target" in msg.lower() or "timed out" in msg.lower():
            _last_result = {
                "ok": False, "status": "close_edge",
                "message": f"Could not attach to {label} — the browser may be running in the background. "
                           "Close it fully and grab again.",
            }
        else:
            _last_result = {"ok": False, "status": "error", "message": f"Browser automation failed: {e}"}
        return _last_result
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass


def reset() -> None:
    global _last_result
    _last_result = {"status": "idle"}


def get_last_result() -> dict:
    return dict(_last_result)
