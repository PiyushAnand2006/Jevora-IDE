"""Structured JSON logging with run/call correlation ids and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
call_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)

# Patterns that look like credentials. Matching text is replaced before a record
# is formatted, so keys never reach any handler.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|x-goog-api-key|authorization|password|secret|token)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record args
            return True
        record.msg = redact(message)
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, var in (("run_id", run_id_var), ("call_id", call_id_var), ("user_id", user_id_var)):
            value = var.get()
            if value:
                payload[key] = value
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k not in payload})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        run_id = run_id_var.get()
        return f"{base} [run={run_id}]" if run_id else base


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_jevora", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._jevora = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter() if json_logs else TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiosqlite", "watchfiles", "mcp", "anyio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_extra(**fields: Any) -> dict[str, Any]:
    """Build the ``extra=`` argument for structured fields."""
    return {"extra_fields": fields}


@contextmanager
def correlation(run_id: str | None = None, call_id: str | None = None) -> Iterator[None]:
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]] = []
    if run_id is not None:
        tokens.append((run_id_var, run_id_var.set(run_id)))
    if call_id is not None:
        tokens.append((call_id_var, call_id_var.set(call_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
