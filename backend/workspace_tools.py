"""Bounded local workspace tools. Paths cannot escape IDE_WORKSPACE_DIR."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Iterable

from .config import WORKSPACE_DIR


_BLOCKED_COMMANDS = (
    re.compile(r"(?i)\brm\s+-[^\n]*r[^\n]*f\b"),
    re.compile(r"(?i)\b(?:del|erase|rmdir|rd)\b[^\n]*(?:/s|/q)"),
    re.compile(r"(?i)\bremove-item\b[^\n]*-(?:recurse|force)"),
    re.compile(r"(?i)(?:~|\$home|%userprofile%|/home/[^\s]+|c:\\users\\[^\s]+)\s*[/\\]\.ssh\b"),
)


def has_tool_permission(tool_permissions: Iterable[str], permission: str) -> bool:
    return permission in set(tool_permissions)


def command_is_safe(command: str) -> bool:
    """Reject commands that can destroy data or read private SSH material."""
    return not any(pattern.search(command) for pattern in _BLOCKED_COMMANDS)


def resolve_path(relative_path: str) -> Path:
    path = (WORKSPACE_DIR / relative_path).resolve()
    if path != WORKSPACE_DIR and WORKSPACE_DIR not in path.parents:
        raise ValueError("Path must stay inside the configured workspace")
    return path


def list_files() -> list[str]:
    ignored = {".git", "node_modules", ".venv", "__pycache__"}
    return [str(p.relative_to(WORKSPACE_DIR)).replace("\\", "/") for p in WORKSPACE_DIR.rglob("*")
            if p.is_file() and not any(part in ignored for part in p.parts)]


def read_file(relative_path: str) -> str:
    return resolve_path(relative_path).read_text(encoding="utf-8")


def write_file(relative_path: str, content: str) -> None:
    path = resolve_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def run_command(command: str, timeout: int = 60) -> dict[str, object]:
    """Run in workspace without shell interpolation; explicitly user-invoked only."""
    if not command_is_safe(command):
        return {"exit_code": -1, "stdout": "", "stderr": "Command blocked by workspace sandbox"}
    proc = await asyncio.create_subprocess_shell(command, cwd=str(WORKSPACE_DIR), stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.communicate()
        return {"exit_code": -1, "stdout": "", "stderr": "Command timed out"}
    return {"exit_code": proc.returncode, "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace")}


async def run_agent_command(tool_permissions: Iterable[str], command: str, timeout: int = 60) -> dict[str, object]:
    """Run a terminal command only for agents explicitly granted terminal access."""
    if not has_tool_permission(tool_permissions, "terminal"):
        return {"exit_code": -1, "stdout": "", "stderr": "Agent is not permitted to use the terminal"}
    return await run_command(command, timeout)
