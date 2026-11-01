"""Shared instructions for the agent runtime."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("system_prompt.md")

# The acting-brain prompt must stay lean: it is sent with every agent call.
# Tool schemas belong to the native ``tools`` API parameter (and the compact
# text-mode tool list), not to this prompt — a past edit embedded the whole
# catalog here, inflating every request to ~80k tokens.
_MAX_PROMPT_CHARS = 60_000


def _load_prompt() -> str:
    """Load the acting-brain prompt, dropping any embedded tool-schema section."""
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("system prompt file missing: %s", PROMPT_PATH)
        return ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "# Tools":
            lines = lines[:i]
            break
    prompt = "\n".join(lines).strip()
    if len(prompt) > _MAX_PROMPT_CHARS:
        log.warning("system prompt is %d chars (cap %d) — truncating; check %s",
                    len(prompt), _MAX_PROMPT_CHARS, PROMPT_PATH)
        prompt = prompt[:_MAX_PROMPT_CHARS]
    return prompt


DEFAULT_ACTING_BRAIN_SYSTEM_PROMPT = _load_prompt()
