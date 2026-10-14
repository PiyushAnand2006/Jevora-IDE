"""Encryption at rest for provider API keys and other secrets (Fernet).

Key resolution order:
1. ``JEVORA_FERNET_KEY`` environment variable / setting.
2. An existing key file ``<data_dir>/secret.key`` (kept for continuity).
3. The OS keyring (service ``jevora-ide``), when enabled and available.
4. A freshly generated key, stored in the keyring if possible, otherwise in the
   key file with 0600 permissions.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.core.logging import get_logger

log = get_logger(__name__)

KEYRING_SERVICE = "jevora-ide"
KEYRING_USERNAME = "fernet-key"


class DecryptionError(Exception):
    pass


class SecretBox:
    def __init__(self, key: bytes, source: str = "unknown") -> None:
        self._fernet = Fernet(key)
        self.source = source

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise DecryptionError("stored secret cannot be decrypted with the current key") from exc


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):  # best effort on Windows
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _keyring_get() -> bytes | None:
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return value.encode("ascii") if value else None
    except Exception as exc:
        log.info("keyring unavailable: %s", type(exc).__name__)
        return None


def _keyring_set(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key.decode("ascii"))
        return _keyring_get() == key
    except Exception as exc:
        log.info("keyring store failed: %s", type(exc).__name__)
        return False


def load_secret_box(data_dir: Path, env_key: str | None = None, use_keyring: bool = True) -> SecretBox:
    if env_key:
        return SecretBox(env_key.encode("ascii"), source="env")
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return SecretBox(key_file.read_bytes().strip(), source="file")
    if use_keyring:
        existing = _keyring_get()
        if existing:
            return SecretBox(existing, source="keyring")
    key = Fernet.generate_key()
    if use_keyring and _keyring_set(key):
        return SecretBox(key, source="keyring")
    _write_private_file(key_file, key)
    return SecretBox(key, source="file")
