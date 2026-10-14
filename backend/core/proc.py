"""Thread-backed subprocess execution.

uvicorn's ``--reload`` mode on Windows runs a SelectorEventLoop, which cannot spawn asyncio
subprocesses. Running ``subprocess.Popen`` in a worker thread works with every event loop, and
lets us stream output, enforce timeouts, and kill whole process trees on cancel.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="jevora-proc")
OutputCallback = Callable[[str, str], None]  # (stream, text) — called from a worker thread


@dataclass
class ProcResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    killed: bool
    duration_s: float
    truncated: bool = False


class ProcHandle:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.killed = False
        self._lock = threading.Lock()

    def kill(self) -> None:
        with self._lock:
            self.killed = True
            proc = self.process
        if proc is not None and proc.poll() is None:
            kill_tree(proc)


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()


def _reader(stream: object, name: str, sink: list[str], limit: int, on_output: OutputCallback | None,
            flags: dict[str, bool]) -> None:
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    size = 0
    read = getattr(stream, "read1", None) or stream.read  # type: ignore[attr-defined]
    while True:
        try:
            chunk = read(4096)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        text = decoder.decode(chunk)
        if not text:
            continue
        if on_output is not None:
            with contextlib.suppress(Exception):  # never let a callback kill the reader
                on_output(name, text)
        if size < limit:
            keep = text[: limit - size]
            sink.append(keep)
            size += len(keep)
            if len(keep) < len(text):
                flags["truncated"] = True
        else:
            flags["truncated"] = True
    tail = decoder.decode(b"", final=True)
    if tail and size < limit:
        sink.append(tail)


def _blocking_run(argv: list[str], cwd: str | None, env: dict[str, str] | None, timeout: float | None,
                  stdin_text: str | None, on_output: OutputCallback | None, max_output: int,
                  handle: ProcHandle) -> ProcResult:
    started = time.monotonic()
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, **kwargs)  # type: ignore[call-overload]
    except (OSError, ValueError) as exc:
        return ProcResult(exit_code=None, stdout="", stderr=f"failed to start process: {exc}", timed_out=False,
                          killed=False, duration_s=0.0)
    with handle._lock:
        handle.process = proc
        if handle.killed:
            kill_tree(proc)
    out: list[str] = []
    err: list[str] = []
    flags = {"truncated": False}
    threads = [threading.Thread(target=_reader, args=(proc.stdout, "stdout", out, max_output, on_output, flags),
                                daemon=True),
               threading.Thread(target=_reader, args=(proc.stderr, "stderr", err, max_output, on_output, flags),
                                daemon=True)]
    for t in threads:
        t.start()
    if stdin_text is not None and proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
    for t in threads:
        t.join(timeout=5)
    return ProcResult(exit_code=proc.returncode, stdout="".join(out), stderr="".join(err), timed_out=timed_out,
                      killed=handle.killed, duration_s=round(time.monotonic() - started, 3),
                      truncated=flags["truncated"])


async def run_process(argv: list[str], *, cwd: str | Path | None = None, env: dict[str, str] | None = None,
                      timeout: float | None = None, stdin: str | None = None,
                      on_output: OutputCallback | None = None, max_output: int = 2_000_000,
                      handle: ProcHandle | None = None) -> ProcResult:
    """Run ``argv`` without a shell. Cancelling the awaiting task kills the process tree."""
    handle = handle or ProcHandle()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_POOL, _blocking_run, argv, str(cwd) if cwd else None, env, timeout, stdin,
                                  on_output, max_output, handle)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        handle.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(future), timeout=15)
        raise


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for untrusted commands: no provider keys or app secrets."""
    keep = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME",
            "USERPROFILE", "LANG", "LC_ALL", "TERM", "PROGRAMFILES", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("TERM", "xterm-256color")
    if extra:
        env.update(extra)
    return env
