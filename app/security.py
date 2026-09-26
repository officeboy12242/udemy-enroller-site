"""Security helpers: password hashing, token encryption, CSRF, rate limiting, sessions."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import config

# ── Password hashing (PBKDF2-HMAC-SHA256) ───────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2$260000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

# ── Token encryption at rest (Fernet derived from SECRET_KEY) ───────────────

def _fernet() -> Fernet:
    digest = hashlib.sha256(config.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()

# ── CSRF tokens (stateless, HMAC-signed with expiry) ────────────────────────

def make_csrf_token() -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    msg = f"{ts}:{nonce}"
    sig = hmac.new(config.CSRF_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def check_csrf_token(token: str, max_age: int = 60 * 60 * 12) -> bool:
    try:
        ts, nonce, sig = token.split(":")
        if hmac.new(config.CSRF_SECRET.encode(), f"{ts}:{nonce}".encode(), hashlib.sha256).hexdigest() != sig:
            return False
        return int(time.time()) - int(ts) <= max_age
    except Exception:
        return False

# ── Signed session cookies (itsdangerous) ───────────────────────────────────
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="uenroller-session")


def sign_session(data: dict) -> str:
    return _serializer.dumps(data)


def read_session(cookie: str, max_age: int = config.SESSION_MAX_AGE):
    """Return session dict or None."""
    if not cookie:
        return None
    try:
        return _serializer.loads(cookie, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):
        return None

# ── Bookmarklet connect codes ───────────────────────────────────────────────
# The bookmarklet runs on udemy.com and POSTs cross-site, where the browser does
# not send our SameSite=Lax session cookie. So it carries this signed code
# instead: it names the site user plus a nonce the user can rotate to revoke it.
_connect_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="uenroller-connect")
CONNECT_CODE_MAX_AGE = 60 * 60 * 24 * 365


def make_connect_code(user_id: int, nonce: str) -> str:
    return _connect_serializer.dumps({"uid": user_id, "n": nonce})


def read_connect_code(code: str) -> dict | None:
    try:
        data = _connect_serializer.loads(code or "", max_age=CONNECT_CODE_MAX_AGE)
        return data if isinstance(data, dict) and "uid" in data else None
    except Exception:
        return None

# ── In-memory rate limiter (per-process; fine for single-instance deploys) ──

class RateLimiter:
    def __init__(self):
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> bool:
        """Record an attempt. Returns True if ALLOWED, False if over limit."""
        now = time.time()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > window:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


login_limiter = RateLimiter()
general_limiter = RateLimiter()
