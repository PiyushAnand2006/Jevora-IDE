"""Password hashing, JWT access tokens, refresh tokens and login rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.core.crypto import _write_private_file
from backend.core.ids import new_id, utcnow

_hasher = PasswordHasher()

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - token type label, not a secret
REFRESH_COOKIE = "jevora_refresh"
OAUTH_STATE_COOKIE = "jevora_oauth_state"


class TokenError(Exception):
    code = "invalid_token"


class TokenExpired(TokenError):
    code = "token_expired"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        # Spend comparable time so missing users are not distinguishable by timing.
        _hasher.hash(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def load_jwt_secret(data_dir: Path, configured: str | None) -> str:
    if configured:
        return configured
    path = data_dir / "jwt.secret"
    if path.exists():
        return path.read_text(encoding="ascii").strip()
    secret = secrets.token_urlsafe(48)
    _write_private_file(path, secret.encode("ascii"))
    return secret


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_in: int


class TokenService:
    algorithm = "HS256"

    def __init__(self, secret: str, access_ttl_minutes: int, refresh_ttl_days: int) -> None:
        self._secret = secret
        self.access_ttl = timedelta(minutes=access_ttl_minutes)
        self.refresh_ttl = timedelta(days=refresh_ttl_days)

    def issue_access(self, user_id: str, ttl: timedelta | None = None) -> IssuedToken:
        now = utcnow()
        lifetime = ttl if ttl is not None else self.access_ttl
        claims = {
            "sub": user_id,
            "type": ACCESS_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int((now + lifetime).timestamp()),
            "jti": new_id(),
        }
        token = jwt.encode(claims, self._secret, algorithm=self.algorithm)
        return IssuedToken(token=token, expires_in=int(lifetime.total_seconds()))

    def decode_access(self, token: str) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(token, self._secret, algorithms=[self.algorithm])
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpired("access token expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenError("invalid access token") from exc
        if claims.get("type") != ACCESS_TOKEN_TYPE or not claims.get("sub"):
            raise TokenError("invalid access token")
        return claims

    def sign_state(self, payload: dict[str, Any], ttl_seconds: int = 600) -> str:
        data = dict(payload)
        data["exp"] = int(time.time()) + ttl_seconds
        data["type"] = "oauth_state"
        return jwt.encode(data, self._secret, algorithm=self.algorithm)

    def verify_state(self, token: str) -> dict[str, Any]:
        try:
            data: dict[str, Any] = jwt.decode(token, self._secret, algorithms=[self.algorithm])
        except jwt.PyJWTError as exc:
            raise TokenError("invalid oauth state") from exc
        if data.get("type") != "oauth_state":
            raise TokenError("invalid oauth state")
        return data


def new_refresh_token() -> tuple[str, str]:
    """Return (raw token for the cookie, sha256 hash for storage)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_otp(code: str, salt: str) -> str:
    """Hash a short-lived OTP with a per-challenge salt."""
    return hmac.new(salt.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_otp(code_hash: str, code: str, salt: str) -> bool:
    return secrets.compare_digest(code_hash, hash_otp(code, salt))


class SlidingWindowRateLimiter:
    """In-memory sliding-window limiter (per process; adequate for a local-first app)."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, float]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.max_events:
                return False, max(0.0, self.window - (now - bucket[0]))
            bucket.append(now)
            return True, 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
