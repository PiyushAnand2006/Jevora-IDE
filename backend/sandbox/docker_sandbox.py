"""Docker sandbox: one long-lived container per run (or per user VM), workspace bind-mounted.

Limits: CPU, memory, PID count, no new privileges, all capabilities dropped, network off by
default, optional gVisor (``runsc``) runtime. Each ``exec`` is wrapped in coreutils ``timeout``
inside the container and tagged with a marker env var so it can be killed precisely on cancel.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shlex
import sys
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT
from backend.core.logging import get_logger
from backend.core.paths import WorkspaceGuard
from backend.core.proc import OutputCallback, ProcHandle, ProcResult, run_process
from backend.sandbox.base import ExecResult, Sandbox, SandboxError

log = get_logger(__name__)
DOCKERFILE = PROJECT_ROOT / "docker" / "sandbox.Dockerfile"


async def docker(*args: str, timeout: float = 60, on_output: OutputCallback | None = None,
                 handle: ProcHandle | None = None) -> ProcResult:
    return await run_process(["docker", *args], timeout=timeout, on_output=on_output, handle=handle)


async def docker_version() -> str | None:
    try:
        result = await docker("version", "--format", "{{.Server.Version}}", timeout=10)
    except OSError:
        return None
    if result.exit_code != 0:
        return None
    return result.stdout.strip() or None


async def gvisor_available() -> bool:
    result = await docker("info", "--format", "{{json .Runtimes}}", timeout=10)
    return result.exit_code == 0 and "runsc" in result.stdout


async def image_exists(image: str) -> bool:
    result = await docker("image", "inspect", image, "--format", "{{.Id}}", timeout=20)
    return result.exit_code == 0


async def build_sandbox_image(image: str, on_output: OutputCallback | None = None) -> bool:
    if not DOCKERFILE.exists():
        return False
    log.info("building sandbox image %s (first use; this can take a few minutes)", image)
    result = await docker("build", "-t", image, "-f", str(DOCKERFILE), str(DOCKERFILE.parent), timeout=1800,
                          on_output=on_output)
    if result.exit_code != 0:
        log.warning("sandbox image build failed: %s", result.stderr[-2000:])
    return result.exit_code == 0


class DockerSandbox(Sandbox):
    kind = "docker"

    def __init__(self, name: str, workspace: Path, image: str, *, runtime: str | None = None, cpus: float = 1.0,
                 memory: str = "1g", pids_limit: int = 256, network: bool = False,
                 labels: dict[str, str] | None = None) -> None:
        super().__init__(name, workspace)
        self.image = image
        self.runtime = runtime
        self.cpus = cpus
        self.memory = memory
        self.pids_limit = pids_limit
        self.network = network
        self.labels = labels or {}
        self.guard = WorkspaceGuard(workspace)
        self._exec_handles: dict[str, ProcHandle] = {}
        self._start_lock = asyncio.Lock()

    async def _state(self) -> str | None:
        result = await docker("inspect", "-f", "{{.State.Status}}", self.name, timeout=20)
        return result.stdout.strip() if result.exit_code == 0 else None

    async def ensure_started(self) -> None:
        async with self._start_lock:
            state = await self._state()
            if state == "running":
                return
            if state is not None:
                result = await docker("start", self.name, timeout=60)
                if result.exit_code == 0:
                    return
                await docker("rm", "-f", self.name, timeout=60)
            args = ["run", "-d", "--name", self.name, "--init",
                    "--mount", f"type=bind,source={self.workspace},target=/workspace",
                    "-w", "/workspace", f"--cpus={self.cpus}", f"--memory={self.memory}",
                    f"--pids-limit={self.pids_limit}", "--security-opt", "no-new-privileges",
                    "--cap-drop", "ALL", "--label", "jevora.sandbox=1",
                    "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1"]
            for key, value in self.labels.items():
                args += ["--label", f"{key}={value}"]
            args += ["--network", "bridge" if self.network else "none"]
            if self.runtime:
                args += ["--runtime", self.runtime]
            if sys.platform != "win32" and hasattr(os, "getuid"):
                args += ["--user", f"{os.getuid()}:{os.getgid()}"]
            args += [self.image, "sleep", "infinity"]
            result = await docker(*args, timeout=180)
            if result.exit_code != 0:
                raise SandboxError(f"could not start sandbox container: {result.stderr.strip()[-500:]}")

    def _container_cwd(self, cwd: str) -> str:
        rel = self.guard.relative(self.guard.resolve(cwd))
        return "/workspace" if rel == "." else f"/workspace/{rel}"

    async def exec(self, command: str, *, cwd: str = ".", timeout: float = 300,
                   env: dict[str, str] | None = None, on_output: OutputCallback | None = None) -> ExecResult:
        await self.ensure_started()
        exec_id = secrets.token_hex(8)
        args = ["exec", "-i", "-w", self._container_cwd(cwd), "-e", f"JEVORA_EXEC_ID={exec_id}"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        seconds = max(1, int(timeout))
        args += [self.name, "timeout", "-k", "5", str(seconds), "sh", "-c", command]
        handle = ProcHandle()
        self._exec_handles[exec_id] = handle
        try:
            result = await docker(*args, timeout=seconds + 20, on_output=on_output, handle=handle)
        except asyncio.CancelledError:
            await self.kill_exec(exec_id)
            raise
        finally:
            self._exec_handles.pop(exec_id, None)
        timed_out = result.timed_out or (result.exit_code in (124, 137) and result.duration_s >= seconds - 1)
        if result.timed_out:
            await self.kill_exec(exec_id)
        return ExecResult(command=command, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                          timed_out=timed_out, killed=result.killed, duration_s=result.duration_s,
                          truncated=result.truncated, sandbox=f"docker:{self.name}")

    async def kill_exec(self, exec_id: str) -> None:
        script = ("for p in /proc/[0-9]*; do "
                  f"if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -qx JEVORA_EXEC_ID={shlex.quote(exec_id)}; "
                  "then kill -9 ${p##*/} 2>/dev/null; fi; done")
        await docker("exec", self.name, "sh", "-c", script, timeout=20)

    async def kill_all(self) -> None:
        for exec_id, handle in list(self._exec_handles.items()):
            handle.kill()
            await self.kill_exec(exec_id)

    def pty_argv(self) -> tuple[list[str], str | None, dict[str, str] | None]:
        return (["docker", "exec", "-it", "-w", "/workspace", self.name, "sh", "-c",
                 "command -v bash >/dev/null 2>&1 && exec bash || exec sh"], None, None)

    async def stop(self) -> None:
        await self.kill_all()
        await docker("rm", "-f", self.name, timeout=60)

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update({"image": self.image, "runtime": self.runtime or "runc", "network": self.network,
                     "cpus": self.cpus, "memory": self.memory, "pids_limit": self.pids_limit,
                     "isolation": "gVisor" if self.runtime == "runsc" else "container"})
        return data
