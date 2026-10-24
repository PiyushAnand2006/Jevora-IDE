"""Shared instructions for the agent runtime."""

from __future__ import annotations

from pathlib import Path

PROMPT_PATH = Path(__file__).with_name("system_prompt.md")


def _load_prompt() -> str:
    """Load the project-owned acting-brain prompt without truncating it."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


DEFAULT_ACTING_BRAIN_SYSTEM_PROMPT = _load_prompt()
