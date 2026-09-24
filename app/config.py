"""Central configuration for the Udemy Enroller site.

All secrets come from environment variables (.env is loaded by run.py).
Every value has a safe default so the app boots even with an empty env,
but production deployments MUST set SECRET_KEY and SITE_ACCESS_CODE.
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

# Code required to register an account on the site (invite-style gate).
SITE_ACCESS_CODE = os.getenv("SITE_ACCESS_CODE", "changeme-access-code")

SESSION_COOKIE_NAME = "uenroller_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30          # 30 days
CSRF_SECRET = SECRET_KEY

# Login rate limiting: max attempts per window per identifier.
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

# ── Auto-enroll engine ──────────────────────────────────────────────────────
AUTO_ENROLL_INTERVAL = int(os.getenv("AUTO_ENROLL_INTERVAL", "600"))   # seconds
AUTO_ENROLL_ENABLED_DEFAULT = os.getenv("AUTO_ENROLL_ENABLED", "1") == "1"
ENROLL_BATCH_LIMIT = int(os.getenv("ENROLL_BATCH_LIMIT", "50"))        # courses per run
FEED_CACHE_TTL = int(os.getenv("FEED_CACHE_TTL", "900"))               # seconds

# ── Server ──────────────────────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8123"))
