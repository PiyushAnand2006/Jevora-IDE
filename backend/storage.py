"""Small, local-first persistence layer for providers, runs, and vault notes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROVIDERS_FILE, ROUTER_CONFIG_FILE, VAULT_DIR, DEFAULT_ROUTER_CONFIG


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"\b(?:sk|nvapi)-[A-Za-z0-9_-]{8,}\b"),
)


def redact_secrets(value: str) -> str:
    """Remove common API-key forms before data leaves the local process."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", redacted)
    return redacted


def _read(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_providers() -> list[dict[str, Any]]:
    return _read(PROVIDERS_FILE, [])


def save_providers(providers: list[dict[str, Any]]) -> None:
    _write(PROVIDERS_FILE, providers)


def load_router_config() -> dict[str, Any]:
    return _read(ROUTER_CONFIG_FILE, DEFAULT_ROUTER_CONFIG)


def save_router_config(config: dict[str, Any]) -> None:
    _write(ROUTER_CONFIG_FILE, config)


def write_run_note(run: dict[str, Any]) -> Path:
    """Write a human-readable Obsidian note and keep all secrets out of it."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    path = VAULT_DIR / "IDE Coder Runs" / f"{stamp}-{run['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    agents = "\n".join(
        f"- `{a.get('parent_id') or 'root'}` → `{a['id']}` — **{a['config']['name']}** ({a['status']})"
        + (f", model: `{a['model_used']}`" if a.get("model_used") else "")
        + f", started: {a.get('started_at', '')}"
        + (f", finished: {a['finished_at']}" if a.get("finished_at") is not None else "")
        for a in run.get("agents", [])
    ) or "- No agents ran"
    decisions = "\n".join(
        f"- **{d['kind']}** → `{d['choice']}` ({d['confidence']:.0%}, {d['engine']})"
        for d in run.get("decisions", [])
    ) or "- No decisions"
    text = f"""---
run_id: {run['id']}
status: {run['status']}
created_at: {run['created_at']}
---

# IDE Coder run

## Task
{run['spec']['text']}

Mode: `{run['spec']['mode']}`

## Agent call tree
{agents}

## Decision evidence and verdicts
{decisions}

## Final output
{redact_secrets(str(run.get('final_output', '')))}
"""
    path.write_text(text, encoding="utf-8")
    return path
