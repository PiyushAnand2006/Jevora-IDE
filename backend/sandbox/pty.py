"""Cross-platform pseudo-terminal sessions (ConPTY via pywinpty on Windows, openpty on POSIX)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from abc import ABC, abstractmethod


class PtySession(ABC):
    @abstractmethod
    def read(self) -> str:
        """Blocking read of available output; returns "" at EOF."""

    @abstractmethod
    def write(self, data: str) -> None: ...

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None: ...

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def exit_code(self) -> int | None: ...


if sys.platform == "win32":  # pragma: no cover - platform specific

    class WinPty(PtySession):  # pragma: no cover - exercised on Windows only
        def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str] | None, cols: int, rows: int) -> None:
            from winpty import PtyProcess

            cmd = subprocess.list2cmdline(argv)
            self._proc = PtyProcess.spawn(cmd, cwd=cwd, env=env, dimensions=(rows, cols))

        def read(self) -> str:
            try:
                data: str = self._proc.read(4096)
                return data
            except EOFError:
                return ""

        def write(self, data: str) -> None:
            self._proc.write(data)

        def resize(self, cols: int, rows: int) -> None:
            self._proc.setwinsize(rows, cols)

        def alive(self) -> bool:
            return bool(self._proc.isalive())

        def close(self) -> None:
            with contextlib.suppress(Exception):
                self._proc.terminate(force=True)

        @property
        def exit_code(self) -> int | None:
            return self._proc.exitstatus if not self._proc.isalive() else None

else:  # pragma: no cover - platform specific

    class PosixPty(PtySession):  # pragma: no cover - exercised on POSIX only
        def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str] | None, cols: int, rows: int) -> None:
            import pty

            master, slave = pty.openpty()
            self._master = master
            self._proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                          start_new_session=True, close_fds=True)
            os.close(slave)
            self.resize(cols, rows)

        def read(self) -> str:
            import select

            while True:
                ready, _, _ = select.select([self._master], [], [], 0.5)
                if ready:
                    try:
                        data = os.read(self._master, 4096)
                    except OSError:
                        return ""
                    return data.decode("utf-8", "replace") if data else ""
                if self._proc.poll() is not None:
                    return ""

        def write(self, data: str) -> None:
            os.write(self._master, data.encode("utf-8"))

        def resize(self, cols: int, rows: int) -> None:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def alive(self) -> bool:
            return self._proc.poll() is None

        def close(self) -> None:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(self._proc.pid), 9)
            with contextlib.suppress(OSError):
                os.close(self._master)

        @property
        def exit_code(self) -> int | None:
            return self._proc.poll()


def spawn_pty(argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, cols: int = 120,
              rows: int = 32) -> PtySession:
    if sys.platform == "win32":
        return WinPty(argv, cwd, env, cols, rows)
    else:
        return PosixPty(argv, cwd, env, cols, rows)
