"""Identifier and time helpers."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime


def new_id(prefix: str = "") -> str:
    """Return a random, URL-safe identifier (optionally prefixed, e.g. ``run_``)."""
    return f"{prefix}{uuid.uuid4().hex}"


def short_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"
