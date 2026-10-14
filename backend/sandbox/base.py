"""Sandbox contract shared by the Docker sandbox and the flagged local dev fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backend.core.proc import OutputCallback


class ExecResult(BaseModel):
    command: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    killed: bool = False
    duration_s: float = 0.0
    truncated: bool = False
    sandbox: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.killed

    def summary(self, limit: int = 6000) -> str:
        def tail(text: str) -> str:
            return text if len(text) <= limit else "…" + text[-limit:]

        parts = [f"$ {self.command}", f"[exit code: {self.exit_code}{' (timed out)' if self.timed_out else ''}"
                 f"{' (killed)' if self.killed else ''}, {self.duration_s:.2f}s]"]
        if self.stdout.strip():
            parts.append("stdout:\n" + tail(self.stdout.rstrip()))
        if self.stderr.strip():
            parts.append("stderr:\n" + tail(self.stderr.rstrip()))
        return "\n".join(parts)


class SandboxError(Exception):
    pass


class Sandbox(ABC):
    kind: str = "abstract"

    def __init__(self, name: str, workspace: Path) -> None:
        self.name = name
        self.workspace = workspace

    @abstractmethod
    async def ensure_started(self) -> None: ...

    @abstractmethod
    async def exec(self, command: str, *, cwd: str = ".", timeout: float = 300,
                   env: dict[str, str] | None = None, on_output: OutputCallback | None = None) -> ExecResult: ...

    @abstractmethod
    def pty_argv(self) -> tuple[list[str], str | None, dict[str, str] | None]:
        """argv, host cwd and env for an interactive PTY session inside this sandbox."""

    @abstractmethod
    async def kill_all(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "workspace": str(self.workspace)}
