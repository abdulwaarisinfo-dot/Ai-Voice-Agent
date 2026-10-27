"""Password hashing, tokens, rate limiting and request checks."""
import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def order_access_token(secret: str, order_id: str) -> str:
    """Unguessable per-order capability, derived so we never have to store it."""
    return hmac.new(secret.encode(), f"order:{order_id}".encode(), hashlib.sha256).hexdigest()[:40]


class RateLimiter:
    """Sliding-window limiter. In-memory => per process. Use Redis if you run several workers."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def client_ip(request: Request) -> str:
    # We do not trust X-Forwarded-For here. Put a trusted proxy in front and configure uvicorn
    # --proxy-headers --forwarded-allow-ips if you deploy behind one.
    return request.client.host if request.client else "unknown"


def require_same_origin(request: Request) -> None:
    """Reject cross-site browser requests to state-changing endpoints."""
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client; cookies/secrets still protect the endpoints that need them
    host = request.headers.get("host", "")
    if origin.split("://", 1)[-1] != host:
        raise HTTPException(403, "Cross-origin request rejected")


def require_owner(request: Request) -> str:
    """Returns the restaurant_id the logged-in owner may manage."""
    rid = request.session.get("owner_restaurant_id")
    if not rid:
        raise HTTPException(401, "Owner login required")
    return rid
