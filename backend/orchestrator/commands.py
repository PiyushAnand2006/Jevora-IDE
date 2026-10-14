"""Slash-command registry: each command routes to a graph node / agent and a mode."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


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
