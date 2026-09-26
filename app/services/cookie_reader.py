"""Direct cookie-DB reader — decrypts Udemy cookies from local browser profiles.

No browser windows and no debugging ports: for each installed Chromium-based
browser, copy the profile's cookie SQLite DB (plus -wal/-shm) and the
"Local State" key file to a private temp dir, then decrypt the Udemy cookies
with the same DPAPI / AES-GCM scheme Chromium uses, right here in Python.

Handles:
  * "v10"/"v11" blobs — AES-256-GCM with the DPAPI-protected key stored in
    Local State (os_crypt.encrypted_key). Covers the vast majority of real
    profiles, including Edge.
  * Legacy blobs encrypted with plain DPAPI (no prefix).
  * "v20" app-bound blobs (Chrome/Edge 127+) are deliberately not decryptable
    outside the browser; they are skipped. The headless browser-probe fallback
    in browser_grab covers those profiles.

Works while the browser is running because we only ever touch copies.
"""
import base64
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
_APPDATA = Path(os.environ.get("APPDATA", ""))

# (display name, user-data dir) — same priority order as browser_grab.
_BROWSER_USER_DATA = [
    ("Microsoft Edge", _LOCALAPPDATA / "Microsoft" / "Edge" / "User Data"),
    ("Google Chrome", _LOCALAPPDATA / "Google" / "Chrome" / "User Data"),
    ("Brave", _LOCALAPPDATA / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ("Vivaldi", _LOCALAPPDATA / "Vivaldi" / "User Data"),
    ("Opera", _APPDATA / "Opera Software" / "Opera Stable"),
]

_SKIP_PROFILE_DIRS = {"System Profile", "Guest Profile", "Crashpad", "ShaderCache"}


def _dpapi_decrypt(data: bytes) -> bytes | None:
    """Decrypt a DPAPI blob (CryptUnprotectData) for the current Windows user."""
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    data_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    data_out = _BLOB()
    descr = ctypes.c_wchar_p()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in), ctypes.byref(descr), None, None, None, 0,
        ctypes.byref(data_out),
    )
    try:
        if not ok:
            return None
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
        if descr.value:
            ctypes.windll.kernel32.LocalFree(descr)


def _load_aes_key(user_data: Path) -> bytes | None:
    """Extract the AES key from Local State (os_crypt.encrypted_key -> DPAPI)."""
    ls = user_data / "Local State"
    if not ls.exists():
        return None
    try:
        data = json.loads(ls.read_text(encoding="utf-8"))
        enc = base64.b64decode(data["os_crypt"]["encrypted_key"])
    except Exception:
        return None
    if not enc.startswith(b"DPAPI"):
        return None
    try:
        return _dpapi_decrypt(enc[5:])
    except Exception:
        return None


def _decrypt_value(blob: bytes, aes_key: bytes | None) -> str:
    """Decrypt one cookie's encrypted_value, or '' if we can't."""
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v11"):
        if not aes_key or len(blob) < 18:
            return ""
        try:
            # nonce = blob[3:15]; ciphertext+GCM tag = blob[15:]
            plain = AESGCM(aes_key).decrypt(blob[3:15], blob[15:], None)
            return plain.decode("utf-8", "replace")
        except Exception:
            return ""
    if blob[:3] == b"v20":
        # App-bound encryption (Chrome/Edge 127+): key lives inside the
        # browser's elevation service — impossible to decrypt externally.
        return ""
    # Legacy: whole blob is DPAPI-encrypted.
    try:
        plain = _dpapi_decrypt(blob)
        return plain.decode("utf-8", "replace") if plain else ""
    except Exception:
        return ""


def _profile_cookie_db(profile_dir: Path) -> Path | None:
    for rel in ("Network" / "Cookies", "Cookies"):
        p = profile_dir / rel
        if p.exists():
            return p
    return None


def _copy_locked(src: Path, dst: Path) -> bool:
    """Copy a file the browser may hold open (copy2, then raw read fallback)."""
    try:
        shutil.copy2(src, dst)
        return True
    except Exception:
        pass
    try:
        dst.write_bytes(src.read_bytes())
        return True
    except Exception:
        return False


def read_udemy_cookies() -> dict:
    """Scan every browser profile's cookie DB for a Udemy session.

    Returns {access_token, client_id, browser} or {}.
    """
    for browser_name, user_data in _BROWSER_USER_DATA:
        if not user_data.exists():
            continue
        aes_key = _load_aes_key(user_data)
        if aes_key is None:
            log.info("No usable os_crypt key in %s Local State", browser_name)

        profiles = []
        try:
            for child in sorted(user_data.iterdir()):
                if not child.is_dir() or child.name in _SKIP_PROFILE_DIRS:
                    continue
                if _profile_cookie_db(child):
                    profiles.append(child)
        except Exception:
            continue
        profiles.sort(key=lambda p: (p.name != "Default", p.name))

        for prof in profiles:
            db = _profile_cookie_db(prof)
            if db is None:
                continue
            tmpdir = Path(tempfile.mkdtemp(prefix="cookie_read_"))
            try:
                # Copy the DB and its WAL sidecars so a live browser is fine.
                for suffix in ("", "-wal", "-shm", "-journal"):
                    src = Path(str(db) + suffix)
                    if src.exists():
                        _copy_locked(src, tmpdir / ("Cookies" + suffix))
                copy = tmpdir / "Cookies"
                if not copy.exists():
                    continue

                try:
                    con = sqlite3.connect(str(copy))  # private copy: WAL replay ok
                    try:
                        rows = con.execute(
                            "SELECT name, encrypted_value FROM cookies "
                            "WHERE host_key LIKE '%udemy.com' "
                            "AND name IN ('access_token','client_id')"
                        ).fetchall()
                    finally:
                        con.close()
                except Exception as e:
                    log.info("Cookie DB unreadable (%s/%s): %s", browser_name, prof.name, e)
                    continue

                values = {}
                for name, blob in rows:
                    val = _decrypt_value(blob or b"", aes_key)
                    if val:
                        values[name] = val
                token = values.get("access_token", "")
                if token:
                    log.info("Found Udemy cookies in %s / %s", browser_name, prof.name)
                    return {
                        "access_token": token,
                        "client_id": values.get("client_id", ""),
                        "browser": browser_name,
                    }
                log.info(
                    "No readable Udemy session in %s / %s (app-bound v20 cookies are skipped)",
                    browser_name, prof.name,
                )
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    return {}


def get_locked_browsers() -> list[str]:
    """Browsers currently holding their cookie DBs locked (i.e. running).

    A running Chromium locks its profile cookie stores, so their sessions
    can't be read directly until the browser is closed. Used to tell the user
    exactly which browser to close instead of failing silently.
    """
    locked = []
    for browser_name, user_data in _BROWSER_USER_DATA:
        if not user_data.exists():
            continue
        dbs = []
        try:
            for child in user_data.iterdir():
                if not child.is_dir() or child.name in _SKIP_PROFILE_DIRS:
                    continue
                db = _profile_cookie_db(child)
                if db:
                    dbs.append(db)
        except Exception:
            continue
        if not dbs:
            continue
        readable = False
        for db in dbs:
            try:
                with open(db, "rb"):
                    readable = True
                    break
            except PermissionError:
                continue
            except Exception:
                # Unreadable for another reason — don't blame a lock.
                readable = True
                break
        if not readable:
            locked.append(browser_name)
    return locked
