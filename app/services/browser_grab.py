"""Server-side Udemy login grab — auto-detects ANY installed browser.

Strategy:
  0. Direct cookie-DB read (no browser windows at all): decrypt the Udemy
     session straight out of every browser profile's cookie store — works
     even while the browser is open.
  1. Enumerate every Chromium-based browser installed for this Windows user
     (Edge, Chrome, Brave, Vivaldi, Opera) and every profile inside each
     (Default, Profile 1, Profile 2, ...).
  2. For each browser+profile, take a lightweight private copy of the profile.
     A cheap name-only cookie check skips profiles with no Udemy login, so we
     only launch a browser where it's worthwhile. The chosen copy is launched
     HEADLESSLY against a debugging port; the browser decrypts its own cookies
     and we read the access_token. The real browser can stay open — the copy
     has its own user-data dir, so nothing is locked and the user is never
     asked to close anything.
  3. If no logged-in Udemy session is found in any browser, open ONE visible
     window (the user's primary browser, its real profile copy) at udemy.com
     so they can log in; the token is captured the moment they do.

Public API:
    grab_from_browser(timeout=180) -> {ok, status, ...}
        status: logged_in | not_logged_in | error | busy
    close_edge() -> bool          (kept for backwards compatibility)
    browser_busy : threading.Event
"""
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from .. import config
from . import cookie_reader
from .udemy_login import verify_token

log = logging.getLogger(__name__)

browser_busy = threading.Event()
_last_result: dict = {"status": "idle"}

UDEMY_URL = "https://www.udemy.com"
_PORT_BASE = 9440       # each probe gets its own port: base + index

_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
_APPDATA = Path(os.environ.get("APPDATA", ""))
_PROGRAMFILES = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
_PROGRAMFILES_X86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))

# Every Chromium-based browser we know how to read. Order = priority for the
# visible fallback window (the user's most likely everyday browser first).
_BROWSER_DEFS = [
    {
        "name": "Microsoft Edge",
        "image": "msedge.exe",
        "exes": [
            _PROGRAMFILES_X86 / r"Microsoft\Edge\Application\msedge.exe",
            _PROGRAMFILES / r"Microsoft\Edge\Application\msedge.exe",
        ],
        "user_data": _LOCALAPPDATA / r"Microsoft\Edge\User Data",
    },
    {
        "name": "Google Chrome",
        "image": "chrome.exe",
        "exes": [
            _PROGRAMFILES / r"Google\Chrome\Application\chrome.exe",
            _PROGRAMFILES_X86 / r"Google\Chrome\Application\chrome.exe",
            _LOCALAPPDATA / r"Google\Chrome\Application\chrome.exe",
        ],
        "user_data": _LOCALAPPDATA / r"Google\Chrome\User Data",
    },
    {
        "name": "Brave",
        "image": "brave.exe",
        "exes": [
            _PROGRAMFILES / r"BraveSoftware\Brave-Browser\Application\brave.exe",
            _PROGRAMFILES_X86 / r"BraveSoftware\Brave-Browser\Application\brave.exe",
            _LOCALAPPDATA / r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        "user_data": _LOCALAPPDATA / r"BraveSoftware\Brave-Browser\User Data",
    },
    {
        "name": "Vivaldi",
        "image": "vivaldi.exe",
        "exes": [
            _LOCALAPPDATA / r"Vivaldi\Application\vivaldi.exe",
            _PROGRAMFILES / r"Vivaldi\Application\vivaldi.exe",
        ],
        "user_data": _LOCALAPPDATA / r"Vivaldi\User Data",
    },
    {
        "name": "Opera",
        "image": "opera.exe",
        "exes": [
            _LOCALAPPDATA / r"Programs\Opera\opera.exe",
            _PROGRAMFILES / r"Opera\opera.exe",
        ],
        "user_data": _APPDATA / r"Opera Software\Opera Stable",
    },
]

_SKIP_PROFILE_DIRS = {"System Profile", "Guest Profile", "Crashpad", "ShaderCache"}


def _first_existing(paths) -> str | None:
    for p in paths:
        try:
            if p and Path(p).exists():
                return str(p)
        except Exception:
            continue
    return None


def _installed_browsers() -> list[dict]:
    """Return the browsers actually installed for this user, exe + user-data."""
    found = []
    for b in _BROWSER_DEFS:
        exe = _first_existing(b["exes"])
        ud = b["user_data"]
        if exe and ud and Path(ud).exists():
            found.append({
                "name": b["name"], "image": b["image"],
                "exe": exe, "user_data": Path(ud),
            })
    return found


def _profile_dirs(user_data: Path) -> list[str]:
    """List profile sub-dirs that actually contain a cookie store."""
    profiles = []
    try:
        for child in sorted(user_data.iterdir()):
            if not child.is_dir() or child.name in _SKIP_PROFILE_DIRS:
                continue
            has_cookies = (child / "Network" / "Cookies").exists() or (child / "Cookies").exists()
            if has_cookies:
                profiles.append(child.name)
    except Exception:
        pass
    # Default first, then Profile N in natural order
    profiles.sort(key=lambda n: (n != "Default", n))
    return profiles or ["Default"]


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
    """Force-close Microsoft Edge. Kept for backwards compatibility with the
    old UI; the new flow no longer needs it because it launches profile copies."""
    try:
        subprocess.run(
            ["taskkill", "/IM", "msedge.exe", "/F"],
            capture_output=True, text=True, timeout=15,
        )
        time.sleep(2)
        return not _process_running("msedge.exe")
    except Exception:
        return False


def close_chrome() -> bool:
    """Force-close Google Chrome (used by the consented close-and-retry flow)."""
    try:
        subprocess.run(
            ["taskkill", "/IM", "chrome.exe", "/F"],
            capture_output=True, text=True, timeout=15,
        )
        time.sleep(2)
        return not _process_running("chrome.exe")
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


def _copy_locked(src: Path, dst: Path) -> None:
    """Copy a file that another process may hold open (browser cookie DB)."""
    try:
        shutil.copy2(src, dst)
        return
    except Exception:
        pass
    # Fallback: read with shared access and write a fresh copy.
    try:
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
    except Exception:
        pass


def _prepare_profile_copy(user_data: Path, profile: str, tag: str) -> str:
    """Copy the minimal files needed to carry a session into a private copy.

    The chosen profile is written into the copy as "Default" so we can launch
    with --profile-directory=Default regardless of its real name. A private
    copy also means the user's real browser can stay open (nothing is locked).
    """
    dst_root = Path(config.DATA_DIR) / "browser_probe" / tag
    shutil.rmtree(dst_root, ignore_errors=True)
    dst_root.mkdir(parents=True, exist_ok=True)

    ls = user_data / "Local State"
    if ls.exists():
        _copy_locked(ls, dst_root / "Local State")

    src_prof = user_data / profile
    dst_prof = dst_root / "Default"
    (dst_prof / "Network").mkdir(parents=True, exist_ok=True)

    # Cookies live under <profile>/Network/Cookies on modern Chromium,
    # or directly under <profile>/Cookies on older builds.
    for rel in (
        "Network/Cookies", "Network/Cookies-wal", "Network/Cookies-shm", "Network/Cookies-journal",
        "Cookies", "Cookies-wal", "Cookies-shm", "Cookies-journal",
    ):
        p = src_prof / rel
        if p.exists():
            target = dst_prof / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_locked(p, target)
    for fname in ("Preferences", "Secure Preferences"):
        p = src_prof / fname
        if p.exists():
            _copy_locked(p, dst_prof / fname)
    return str(dst_root)


def _open_page(browser_path, user_data, port, headless):
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    if browser_path:
        co.set_browser_path(browser_path)
    co.set_local_port(port)
    co.set_user_data_path(user_data)
    co.set_argument("--profile-directory=Default")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--disable-crash-reporter")
    co.set_argument("--disable-sync")
    if headless:
        co.headless(True)
    return ChromiumPage(co)


def _open_login_page(browser_path):
    """Open a small, visible, ISOLATED browser window for the user to log in.

    auto_port() gives a random free debug port plus its own throwaway profile,
    so this window runs happily alongside the user's already-open browser
    (which otherwise causes DrissionPage's BrowserConnectError).
    """
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    if browser_path:
        co.set_browser_path(browser_path)
    co.auto_port(True)
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--disable-crash-reporter")
    co.set_argument("--new-window")
    co.set_argument("--window-size=520,760")
    co.headless(False)
    return ChromiumPage(co)


# ── Fast pre-filter: does this profile even have a Udemy login? ──────────────
# We only read cookie NAMES here (never the encrypted values), purely to decide
# whether launching the browser is worthwhile. The browser itself does all the
# actual decryption when we launch it against the copy.

def _cookies_db_path(copy_root: str) -> Path | None:
    root = Path(copy_root) / "Default"
    for rel in ("Network/Cookies", "Cookies"):
        p = root / rel
        if p.exists():
            return p
    return None


def _profile_has_udemy_login(copy_root: str) -> bool:
    """True if the copied profile has a Udemy access_token cookie present.

    Reads only the cookie name/host columns — not the secret value — so we can
    skip launching a browser for profiles with no Udemy session at all.
    """
    db = _cookies_db_path(copy_root)
    if not db:
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM cookies "
                "WHERE host_key LIKE '%udemy.com' AND name='access_token' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except Exception:
        # If we can't read it, don't skip — let the browser launch decide.
        return True


def _probe_browser(browser: dict, copy_root: str, port: int) -> dict | None:
    """Launch the prepared profile copy headlessly and read its Udemy token.

    Returns a logged_in result dict, or None if no valid token there.
    """
    label = browser["name"]
    page = None
    try:
        page = _open_page(browser["exe"], copy_root, port, headless=True)
        # Cookies are loaded from the profile into the network service at launch;
        # poll briefly, then read without needing to navigate anywhere.
        deadline = time.time() + 6
        cookies = {}
        while time.time() < deadline:
            try:
                cookies = _read_cookies(page)
            except Exception:
                cookies = {}
            if cookies.get("access_token"):
                break
            time.sleep(0.3)

        token = cookies.get("access_token", "")
        if not token:
            log.info("No Udemy session read from %s", label)
            return None

        info = verify_token(token, cookies.get("client_id", ""))
        if not info.get("valid"):
            log.info("Token in %s rejected by Udemy (%s)", label, info.get("error"))
            return None

        log.info("Udemy login found in %s (%s)", label, info.get("name"))
        return {
            "ok": True, "status": "logged_in",
            "access_token": token,
            "client_id": info.get("client_id") or cookies.get("client_id", ""),
            "udemy_user_id": info.get("udemy_user_id"),
            "name": info.get("name"),
            "source": browser["name"],
        }
    except Exception as e:
        log.info("Probe of %s failed: %s", label, e)
        return None
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass


def _interactive_login(browser: dict, timeout: int) -> dict:
    """Open ONE visible window at udemy.com and wait for the user to log in."""
    global _last_result
    label = browser["name"]
    login_url = "https://www.udemy.com/join/login-popup/?locale=en_US&next=https%3A%2F%2Fwww.udemy.com%2F"
    page = None
    try:
        page = _open_login_page(browser["exe"])
    except Exception as e:
        # Launch/connect failure — signal the caller to try another browser.
        log.info("Could not open a login window in %s: %s", label, e)
        return {"ok": False, "status": "launch_failed", "message": str(e)}

    try:
        _last_result = {
            "ok": False, "status": "waiting_login",
            "message": f"A {label} window opened at Udemy. Log in there — password, "
                       "captcha, OTP, all of it — and we'll connect your account automatically.",
        }
        try:
            page.get(login_url)
            page.wait.doc_loaded(timeout=40)
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cookies = _read_cookies(page)
            except Exception:
                # window was closed by the user
                _last_result = {
                    "ok": False, "status": "not_logged_in",
                    "message": "The login window was closed before sign-in completed. "
                               "Click Connect again and finish logging in.",
                }
                return _last_result
            token = cookies.get("access_token", "")
            if token:
                info = verify_token(token, cookies.get("client_id", ""))
                if info.get("valid"):
                    _last_result = {
                        "ok": True, "status": "logged_in",
                        "access_token": token,
                        "client_id": info.get("client_id") or cookies.get("client_id", ""),
                        "udemy_user_id": info.get("udemy_user_id"),
                        "name": info.get("name"),
                        "source": label,
                    }
                    return _last_result
            time.sleep(2)

        _last_result = {
            "ok": False, "status": "not_logged_in",
            "message": f"Didn't detect a Udemy login in the {label} window in time. "
                       "Click Connect again and finish signing in.",
        }
        return _last_result
    except Exception as e:
        log.exception("interactive login failed (%s)", label)
        return {"ok": False, "status": "launch_failed", "message": f"Browser automation failed: {e}"}
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass


def _try_direct_read(browsers: list[dict]) -> dict | None:
    """Phase 0 — direct cookie-DB read (no windows, no debugging ports).

    Returns a terminal result (logged_in, or a close_browsers hint when a
    running browser locks its cookie store) or None to continue the normal
    flow.
    """
    try:
        direct = cookie_reader.read_udemy_cookies()
    except Exception as e:
        log.info("Direct cookie read failed: %s", e)
        direct = {}
    if direct.get("access_token"):
        info = verify_token(direct["access_token"], direct.get("client_id", ""))
        if info.get("valid"):
            return {
                "ok": True, "status": "logged_in",
                "access_token": direct["access_token"],
                "client_id": info.get("client_id") or direct.get("client_id", ""),
                "udemy_user_id": info.get("udemy_user_id"),
                "name": info.get("name"),
                "source": direct.get("browser", "browser"),
            }
        log.info("Direct read got a token but Udemy rejected it (%s)", info.get("error"))
    locked = cookie_reader.get_locked_browsers()
    if locked:
        names = ", ".join(locked)
        return {
            "ok": False, "status": "close_browsers",
            "message": f"Your Udemy login is inside {names}, which is open right now "
                       "and keeps its cookies locked. Close it and click Retry — "
                       "no login needed.",
            "locked_browsers": locked,
        }
    return None


def grab_from_browser(timeout: int = 180) -> dict:
    global _last_result
    if browser_busy.is_set():
        res = {"ok": False, "status": "busy", "message": "A browser session is already running."}
        _last_result = res
        return res
    browser_busy.set()
    try:
        browsers = _installed_browsers()
        if not browsers:
            _last_result = {
                "ok": False, "status": "error",
                "message": "No supported browser found (Edge, Chrome, Brave, Vivaldi, or Opera).",
            }
            return _last_result

        log.info("Detected browsers: %s", ", ".join(b["name"] for b in browsers))

        # Phase 0 — direct cookie-DB read: no windows, no debugging ports.
        # Decrypts the Udemy session straight out of each browser's cookie
        # store; nothing is launched, so it works while the browser is open.
        _last_result = {
            "ok": False, "status": "scanning",
            "message": "Checking your browsers for an existing Udemy login…",
        }
        direct = _try_direct_read(browsers)
        if direct:
            _last_result = direct
            return direct

        # Phase 1 — check every browser + profile for an existing login.
        # Copy each profile, and only launch a browser for the ones that
        # actually hold a Udemy access_token cookie (cheap name-only check),
        # so we never waste time booting a browser for an unrelated profile.
        idx = 0
        for browser in browsers:
            for profile in _profile_dirs(browser["user_data"]):
                _last_result = {
                    "ok": False, "status": "scanning",
                    "message": f"Checking {browser['name']} ({profile})…",
                }
                tag = f"{browser['image']}_{profile}".replace(" ", "_").replace("\\", "_")
                try:
                    copy_root = _prepare_profile_copy(browser["user_data"], profile, tag)
                except Exception as e:
                    log.info("Could not copy %s / %s: %s", browser["name"], profile, e)
                    continue
                if not _profile_has_udemy_login(copy_root):
                    log.info("No Udemy cookie in %s / %s — skipping", browser["name"], profile)
                    continue
                idx += 1
                result = _probe_browser(browser, copy_root, _PORT_BASE + idx)
                if result:
                    _last_result = result
                    return result

        # Phase 2 — nothing found: open a visible window in the primary browser.
        return _interactive_login(browsers[0], timeout)
    finally:
        browser_busy.clear()


def open_login_window(timeout: int = 300) -> dict:
    """Open a real Udemy login window and wait for the user to sign in.

    The user handles password, captcha and OTP themselves in the window; as soon
    as a valid session appears we capture the token and return logged_in.
    """
    global _last_result
    if browser_busy.is_set():
        res = {"ok": False, "status": "busy", "message": "A login window is already open."}
        _last_result = res
        return res
    browser_busy.set()
    try:
        browsers = _installed_browsers()
        if not browsers:
            _last_result = {
                "ok": False, "status": "error",
                "message": "No supported browser found (Edge, Chrome, Brave, Vivaldi, or Opera).",
            }
            return _last_result

        # Phase 0 — direct cookie-DB read: no windows, no debugging ports.
        # Decrypts the Udemy session straight out of each browser's cookie
        # store; nothing is launched, so it works while the browser is open.
        _last_result = {
            "ok": False, "status": "scanning",
            "message": "Checking your browsers for an existing Udemy login…",
        }
        direct = _try_direct_read(browsers)
        if direct:
            _last_result = direct
            return direct

        # Prefer a browser that isn't already running, to avoid launch conflicts.
        ordered = sorted(browsers, key=lambda b: _process_running(b["image"]))
        log.info("Opening Udemy login window; browser order: %s",
                 ", ".join(b["name"] for b in ordered))

        _last_result = {"ok": False, "status": "opening", "message": "Opening a Udemy login window…"}
        last = None
        for browser in ordered:
            res = _interactive_login(browser, timeout)
            if res.get("ok"):
                return res
            last = res
            # A real login attempt happened (user closed/timed out) — don't reopen.
            if res.get("status") == "not_logged_in":
                return res
            # Otherwise it was a launch failure; try the next browser.
        _last_result = last or {
            "ok": False, "status": "error",
            "message": "Could not open a login window in any installed browser.",
        }
        return _last_result
    finally:
        browser_busy.clear()


def reset() -> None:
    global _last_result
    _last_result = {"status": "idle"}


def get_last_result() -> dict:
    return dict(_last_result)
