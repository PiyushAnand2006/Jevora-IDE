"""Slash-command registry: each command routes to a graph node / agent and a mode.

Projects can add their own commands as ``.jevora/commands/<name>.md`` in the
workspace: the file body becomes the prompt (``{{args}}`` expands to whatever
follows the command), and an optional first line ``mode: build|chat|research|analysis``
routes it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CUSTOM_COMMANDS_DIR = Path(".jevora") / "commands"
_CUSTOM_NAME_RE = re.compile(r"[a-z0-9_-]+")
_MODE_LINE_RE = re.compile(r"mode:\s*(chat|build|research|analysis)", re.I)


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    mode: str
    node: str
    args_hint: str = ""


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/test", "Run the test suite; with a URL, also drive the app with the screen-control agent",
                 "build", "tester", "[url] [what to check]"),
    SlashCommand("/fix", "Fix a bug: reproduce it with a failing test, then fix and verify", "build", "coder",
                 "<bug description>"),
    SlashCommand("/design", "Design and implement UI (creative-UI model tier)", "build", "ui_designer",
                 "<what to design>"),
    SlashCommand("/research", "Research a topic with numbered, cited sources", "research", "researcher",
                 "<question>"),
    SlashCommand("/explain", "Explain code or a concept using this workspace", "chat", "chat", "<topic or file>"),
    SlashCommand("/review", "Code-review the current uncommitted changes", "build", "critic", "[focus]"),
    SlashCommand("/plan", "Produce an implementation plan only (no code changes)", "build", "planner", "<task>"),
    SlashCommand("/run", "Run a shell command in the sandbox (approval policy applies)", "build", "run_command",
                 "<command>"),
    SlashCommand("/analyze", "Analyse the repository: structure, dependencies, complexity", "analysis", "analyst",
                 "[focus]"),
)
BY_NAME = {c.name: c for c in COMMANDS}


def parse_command(text: str, mode: str) -> tuple[str | None, str, str]:
    """Split ``/cmd rest`` into (command, mode, rest). Unknown commands are left as plain text."""
    stripped = text.lstrip()
    head, _, rest = stripped.partition(" ")
    command = BY_NAME.get(head.lower())
    if command is None:
        return None, mode, text
    return command.name, command.mode, rest.strip() or command.description


def command_list() -> list[dict[str, Any]]:
    return [asdict(c) for c in COMMANDS]


def load_workspace_command(workspace: Path, text: str) -> tuple[str, str] | None:
    """Match ``/name args`` against ``.jevora/commands/<name>.md``.

    Returns ``(rendered prompt, mode)`` or ``None`` when the command is not a
    project-defined one.
    """
    stripped = text.lstrip()
    head, _, rest = stripped.partition(" ")
    if not head.startswith("/") or len(head) < 2:
        return None
    name = head[1:].lower()
    if not _CUSTOM_NAME_RE.fullmatch(name):
        return None
    path = workspace / CUSTOM_COMMANDS_DIR / f"{name}.md"
    try:
        if not path.is_file():
            return None
        body = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    mode = "build"
    lines = body.splitlines()
    if lines and _MODE_LINE_RE.fullmatch(lines[0].strip()):
        mode = _MODE_LINE_RE.search(lines[0]).group(1).lower()
        body = "\n".join(lines[1:]).strip()
    args = rest.strip()
    if "{{args}}" in body:
        prompt = body.replace("{{args}}", args)
    else:
        prompt = body + (f"\n\nUser arguments: {args}" if args else "")
    return prompt, mode


def workspace_command_list(workspace: Path) -> list[dict[str, Any]]:
    """Custom commands for the UI's slash menu."""
    out: list[dict[str, Any]] = []
    directory = workspace / CUSTOM_COMMANDS_DIR
    try:
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.md")):
            name = path.stem.lower()
            if not _CUSTOM_NAME_RE.fullmatch(name):
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [line for line in body.splitlines() if line.strip()]
            description = lines[0].lstrip("# ").strip() if lines else ""
            if _MODE_LINE_RE.fullmatch(description):
                description = lines[1].lstrip("# ").strip() if len(lines) > 1 else ""
            out.append({"name": f"/{name}", "description": description[:120], "args_hint": "args",
                        "custom": True})
    except OSError:
        return out
    return out
