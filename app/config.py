"""Central configuration for the Udemy Enroller site.

All secrets come from environment variables (.env is loaded by run.py).
Every value has a safe default so the app boots even with an empty env,
but production deployments MUST set SECRET_KEY.
"""
import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv  # optional convenience
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = "Udemy Enroller"

# ── Security ────────────────────────────────────────────────────────────────
# Signs session cookies AND derives the Fernet key that encrypts Udemy tokens
# at rest. A random key is generated once and persisted if not provided.
_secret_file = DATA_DIR / ".secret_key"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if _secret_file.exists():
        SECRET_KEY = _secret_file.read_text().strip()
    else:
        SECRET_KEY = secrets.token_urlsafe(48)
        _secret_file.write_text(SECRET_KEY)
        try:
            _secret_file.chmod(0o600)
        except Exception:
            pass

SESSION_COOKIE_NAME = "uenroller_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30          # 30 days
CSRF_SECRET = SECRET_KEY
# Set to 1 when serving over HTTPS so the session cookie is marked Secure.
SESSION_SECURE = os.getenv("SESSION_SECURE", "0") == "1"

# Login rate limiting: max attempts per window per identifier.
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

# ── Auto-enroll engine ──────────────────────────────────────────────────────
# 120s / 2 min, matching the tgbot2 reference bot's AUTO_ENROLL_INTERVAL.
AUTO_ENROLL_INTERVAL = int(os.getenv("AUTO_ENROLL_INTERVAL", "120"))   # seconds
AUTO_ENROLL_ENABLED_DEFAULT = os.getenv("AUTO_ENROLL_ENABLED", "1") == "1"
# No cap on courses per run - every free course the feed finds is attempted.
FEED_CACHE_TTL = int(os.getenv("FEED_CACHE_TTL", "900"))               # seconds
# Stop paging the feed once listings are older than this - their coupons are dead.
FEED_MAX_AGE_HOURS = int(os.getenv("FEED_MAX_AGE_HOURS", "96"))

# ── Server ──────────────────────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8123"))

# ── Hosting ─────────────────────────────────────────────────────────────────
# Render sets RENDER=true and RENDER_EXTERNAL_URL automatically.
ON_RENDER = bool(os.getenv("RENDER"))
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
# The "open a Udemy login window" flow drives a real browser on the machine
# running the app, so it only makes sense when that machine is your own PC.
LOCAL_BROWSER_LOGIN = os.getenv("LOCAL_BROWSER_LOGIN", "0" if ON_RENDER else "1") == "1"
# Self-ping so a free Render instance never idles out and stops auto-enroll
# (same idea as tgbot2's KEEPALIVE_INTERVAL). 0 disables it.
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "600" if PUBLIC_URL else "0"))
