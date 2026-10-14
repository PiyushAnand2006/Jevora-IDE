from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

import pytest

from backend.core.crypto import DecryptionError, SecretBox, load_secret_box
from backend.core.logging import RedactingFilter, redact
from backend.core.security import (
    SlidingWindowRateLimiter,
    TokenError,
    TokenExpired,
    TokenService,
    hash_password,
    verify_password,
)


def test_secret_box_round_trip_and_key_file(tmp_path: Path) -> None:
    box = load_secret_box(tmp_path, use_keyring=False)
    token = box.encrypt("sk-live-very-secret")
    assert token != "sk-live-very-secret" and "very-secret" not in token
    assert box.decrypt(token) == "sk-live-very-secret"
    # The key persists in a private file and is reused on the next start.
    key_file = tmp_path / "secret.key"
    assert key_file.exists()
    again = load_secret_box(tmp_path, use_keyring=False)
    assert again.decrypt(token) == "sk-live-very-secret"
    assert again.source == "file"


def test_decrypt_with_wrong_key_fails(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    token = SecretBox(Fernet.generate_key()).encrypt("x")
    with pytest.raises(DecryptionError):
        SecretBox(Fernet.generate_key()).decrypt(token)


def test_env_key_takes_precedence(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    box = load_secret_box(tmp_path, env_key=key, use_keyring=False)
    assert box.source == "env"
    assert not (tmp_path / "secret.key").exists()


def test_password_hashing() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong")
    assert not verify_password(None, "anything")


def test_access_tokens_and_expiry() -> None:
    svc = TokenService("s" * 48, 15, 14)
    issued = svc.issue_access("usr_1")
    assert svc.decode_access(issued.token)["sub"] == "usr_1"
    expired = svc.issue_access("usr_1", ttl=timedelta(seconds=-5))
    with pytest.raises(TokenExpired):
        svc.decode_access(expired.token)
    with pytest.raises(TokenError):
        svc.decode_access(issued.token + "tampered")
    with pytest.raises(TokenError):
        TokenService("other-secret-" * 4, 15, 14).decode_access(issued.token)


def test_rate_limiter_window() -> None:
    limiter = SlidingWindowRateLimiter(2, 0.2)
    assert limiter.hit("k")[0] and limiter.hit("k")[0]
    allowed, retry = limiter.hit("k")
    assert not allowed and retry > 0
    time.sleep(0.25)
    assert limiter.hit("k")[0]


def test_log_redaction(caplog: pytest.LogCaptureFixture) -> None:
    assert "sk-abcdefghijklmnop" not in redact("key=sk-abcdefghijklmnop")
    assert "[REDACTED]" in redact('{"api_key": "plain-secret-value"}')
    assert "AIzaSyA1234567890abcdefghijk" not in redact("x-goog-api-key: AIzaSyA1234567890abcdefghijk")
    logger = logging.getLogger("redaction-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="redaction-test"):
        logger.info("calling provider with Authorization: Bearer sk-supersecretkey123456")
    assert "sk-supersecretkey123456" not in caplog.text
