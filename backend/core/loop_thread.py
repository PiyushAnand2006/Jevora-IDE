"""A dedicated event loop in a background thread, able to spawn subprocesses on every platform.

Playwright launches its driver as a subprocess. Under uvicorn ``--reload`` on Windows the main loop is
a SelectorEventLoop, which cannot do that, so Playwright work is submitted to this Proactor loop.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


class LoopThread:
    def __init__(self, name: str = "jevora-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def _run(self) -> None:
        loop: asyncio.AbstractEventLoop = (asyncio.ProactorEventLoop() if sys.platform == "win32"
                                           else asyncio.new_event_loop())
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    def ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
                self._ready.wait(10)
        assert self._loop is not None
        return self._loop

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        loop = self.ensure_started()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    async def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Await ``coro`` on the background loop from any other loop."""
        future = self.submit(coro)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            future.cancel()
            raise

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        self._loop = None
