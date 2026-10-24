"""Small, dependency-free helpers shared by vault notes and attachments."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[\\/:*?"<>|#\[\]\^]')
_WHITESPACE = re.compile(r"\s+")


def safe_filename(value: str, max_length: int = 80) -> str:
    """Return a filesystem-safe, human-readable filename component.

    Obsidian vaults are commonly synced across platforms, so the conservative
    Windows-invalid character set is used even when Jevora runs on POSIX.
    """

    cleaned = _UNSAFE.sub(" ", value or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        return "untitled"
    return cleaned[:max_length].rstrip(" .") or "untitled"
