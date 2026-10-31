"""LOCAL_UNSAFE_EXEC dev fallback: runs commands directly on the host, inside the workspace directory.

This is NOT an isolation boundary. It exists so the IDE works on machines without Docker; it is
only used when ``LOCAL_UNSAFE_EXEC=true`` and is reported as such by ``/api/system/health``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from backend.core.paths import WorkspaceGuard
from backend.core.proc import OutputCallback, ProcHandle, run_process, scrubbed_env
from backend.sandbox.base import ExecResult, Sandbox
from backend.sandbox.netfilter import DomainAllowProxy


def shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def local_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Scrubbed env whose PATH starts with this interpreter's directory, so `python` means our venv."""
    env = scrubbed_env(extra)
    interpreter_dir = str(Path(sys.executable).parent)
    env["PATH"] = interpreter_dir + os.pathsep + env.get("PATH", "")
    return env


def interactive_shell() -> list[str]:
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        return [pwsh, "-NoLogo"] if pwsh else [os.environ.get("COMSPEC", "cmd.exe")]
    return [os.environ.get("SHELL") or "/bin/bash", "-i"]


class LocalSandbox(Sandbox):
    kind = "local"

    def __init__(self, name: str, workspace: Path, *, allowed_domains: list[str] | None = None) -> None:
        super().__init__(name, workspace)
        self.guard = WorkspaceGuard(workspace)
        self._handles: set[ProcHandle] = set()
        self.allowed_domains = list(allowed_domains or [])
        self._proxy: DomainAllowProxy | None = None

    async def _proxy_env(self) -> dict[str, str]:
        """Standard proxy env when a domain allow-list is configured (loopback bypassed)."""
        if not self._proxy:
            return {}
        url = f"http://127.0.0.1:{self._proxy.listen_port}"
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url,
                "NO_PROXY": "localhost,127.0.0.1,::1"}

    async def ensure_started(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.allowed_domains and self._proxy is None:
            self._proxy = DomainAllowProxy(self.allowed_domains)
            await self._proxy.start()

    async def exec(self, command: str, *, cwd: str = ".", timeout: float = 300,
                   env: dict[str, str] | None = None, on_output: OutputCallback | None = None) -> ExecResult:
        await self.ensure_started()
        workdir = self.guard.resolve(cwd)
        handle = ProcHandle()
        self._handles.add(handle)
        merged = {**(env or {}), **(await self._proxy_env())}
        try:
            result = await run_process(shell_argv(command), cwd=workdir, env=local_env(merged), timeout=timeout,
                                       on_output=on_output, handle=handle)
        finally:
            self._handles.discard(handle)
        return ExecResult(command=command, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                          timed_out=result.timed_out, killed=result.killed, duration_s=result.duration_s,
                          truncated=result.truncated, sandbox=f"local:{self.name}")

    def pty_argv(self) -> tuple[list[str], str | None, dict[str, str] | None]:
        proxy_env: dict[str, str] = {}
        if self._proxy is not None:
            url = f"http://127.0.0.1:{self._proxy.listen_port}"
            proxy_env = {"HTTP_PROXY": url, "HTTPS_PROXY": url,
                         "NO_PROXY": "localhost,127.0.0.1,::1"}
        return interactive_shell(), str(self.workspace), {**local_env(), **proxy_env}

    async def kill_all(self) -> None:
        for handle in list(self._handles):
            handle.kill()

    async def stop(self) -> None:
        await self.kill_all()
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None

    def describe(self) -> dict[str, object]:
        data = super().describe()
        data["isolation"] = "none (LOCAL_UNSAFE_EXEC)"
        data["allowed_domains"] = self.allowed_domains
        return data
