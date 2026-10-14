"""Background run executor: bounded concurrency, per-user round-robin fairness, cancel, recovery."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.events import TERMINAL_STATUSES, EventBus
from backend.core.logging import correlation, get_logger
from backend.core.metrics import Metrics
from backend.db.models import Run
from backend.orchestrator.recorder import RunRecorder

log = get_logger(__name__)

RESUMABLE_STATUSES = ("queued", "planning", "running", "verifying", "awaiting_user")


@dataclass
class RunHandle:
    run_id: str
    user_id: str
    recorder: RunRecorder
    resume: Any = None  # None = fresh start; "restart" = resume from checkpoint; dict = escalation answer
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


Runner = Callable[[RunHandle], Awaitable[None]]
CancelHook = Callable[[str], Awaitable[None]]


class RunExecutor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventBus, max_concurrent: int,
                 metrics: Metrics | None = None) -> None:
        self.sessions = sessions
        self.events = events
        self.metrics = metrics
        self.max_concurrent = max(1, max_concurrent)
        self.runner: Runner | None = None
        self.cancel_hooks: list[CancelHook] = []
        self.finish_hooks: list[Callable[[str], Awaitable[None]]] = []
        self._pending: dict[str, deque[tuple[str, Any]]] = {}
        self._rr: deque[str] = deque()
        self._active_by_user: dict[str, int] = {}
        self._last_served: dict[str, int] = {}
        self._serve_counter = 0
        self._wake = asyncio.Event()
        self._slots = asyncio.Semaphore(self.max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handles: dict[str, RunHandle] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="run-dispatcher")

    async def stop(self) -> None:
        """Shutdown: stop dispatching and interrupt active runs, leaving their status for recovery."""
        self._stopping = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recover(self) -> list[str]:
        """Resubmit runs that were in flight when the process stopped (resume from checkpoint)."""
        async with self.sessions() as session:
            rows = await session.execute(
                select(Run.id, Run.user_id, Run.status).where(Run.status.in_(RESUMABLE_STATUSES))
                .order_by(Run.created_at))
            items = rows.all()
        resumed: list[str] = []
        for run_id, user_id, status in items:
            self.submit(run_id, user_id, resume=None if status == "queued" else "restart")
            resumed.append(run_id)
        if resumed:
            log.info("recovered %d in-flight runs", len(resumed))
        return resumed

    # ------------------------------------------------------------------ submit / cancel
    def submit(self, run_id: str, user_id: str, resume: Any = None) -> None:
        if run_id in self._tasks or any(run_id == r for q in self._pending.values() for r, _ in q):
            return
        queue = self._pending.setdefault(user_id, deque())
        queue.append((run_id, resume))
        if user_id not in self._rr:
            self._rr.append(user_id)
        self._wake.set()

    def is_active(self, run_id: str) -> bool:
        return run_id in self._tasks or any(run_id == r for q in self._pending.values() for r, _ in q)

    def handle(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def queued_count(self) -> int:
        return sum(len(q) for q in self._pending.values())

    async def cancel(self, run_id: str) -> bool:
        for user, queue in list(self._pending.items()):
            for item in list(queue):
                if item[0] == run_id:
                    queue.remove(item)
                    if not queue:
                        self._pending.pop(user, None)
                        with contextlib.suppress(ValueError):
                            self._rr.remove(user)
        handle = self._handles.get(run_id)
        if handle is not None:
            handle.cancel_event.set()
        for hook in self.cancel_hooks:
            try:
                await hook(run_id)
            except Exception:
                log.exception("cancel hook failed for %s", run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=15)
        return True

    # ------------------------------------------------------------------ internals
    async def _dispatch_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._rr:
                await self._slots.acquire()
                if not self._rr:
                    self._slots.release()
                    break
                # Fair share: the user with the fewest active runs goes first; ties go to whoever
                # was served least recently, so one user's backlog cannot starve another's.
                user = min(self._rr, key=lambda u: (self._active_by_user.get(u, 0), self._last_served.get(u, -1)))
                self._rr.remove(user)
                queue = self._pending.get(user)
                if not queue:
                    self._pending.pop(user, None)
                    self._slots.release()
                    continue
                run_id, resume = queue.popleft()
                if queue:
                    self._rr.append(user)
                else:
                    self._pending.pop(user, None)
                self._serve_counter += 1
                self._last_served[user] = self._serve_counter
                self._active_by_user[user] = self._active_by_user.get(user, 0) + 1
                handle = RunHandle(run_id=run_id, user_id=user,
                                   recorder=RunRecorder(self.sessions, self.events, run_id, user, self.metrics),
                                   resume=resume)
                self._handles[run_id] = handle
                self._tasks[run_id] = asyncio.create_task(self._execute(handle), name=f"run-{run_id}")

    async def _execute(self, handle: RunHandle) -> None:
        run_id = handle.run_id
        if self.metrics is not None:
            self.metrics.runs_active.inc()
        try:
            with correlation(run_id=run_id):
                if self.runner is None:
                    raise RuntimeError("no orchestrator runner configured")
                await self.runner(handle)
        except asyncio.CancelledError:
            if handle.cancelled:
                await self._finalize_cancelled(handle)
            else:
                log.info("run %s interrupted by shutdown; it will resume on next start", run_id)
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            try:
                await handle.recorder.cancel_open_calls("run crashed")
                await handle.recorder.set_status("failed", error=f"{type(exc).__name__}: {exc}")
            except Exception:
                log.exception("could not mark run %s failed", run_id)
        finally:
            self._tasks.pop(run_id, None)
            self._handles.pop(run_id, None)
            self._active_by_user[handle.user_id] = max(0, self._active_by_user.get(handle.user_id, 1) - 1)
            self._slots.release()
            if self.metrics is not None:
                self.metrics.runs_active.dec()
            if not self._stopping:
                await self._after_run(run_id)

    async def _finalize_cancelled(self, handle: RunHandle) -> None:
        await handle.recorder.cancel_open_calls("cancelled by user")
        async with self.sessions() as session:
            run = await session.get(Run, handle.run_id)
            already_terminal = run is not None and run.status in TERMINAL_STATUSES and run.cancelled
        if not already_terminal:
            await handle.recorder.set_status("failed", error="cancelled by user", cancelled=True,
                                             final_output="Run cancelled by user.")

    async def _after_run(self, run_id: str) -> None:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            status, mode = (run.status, run.mode) if run is not None else (None, None)
        if status is not None and status in TERMINAL_STATUSES and self.metrics is not None:
            self.metrics.runs.labels(mode, status).inc()
        for hook in self.finish_hooks:
            try:
                await hook(run_id)
            except Exception:
                log.exception("finish hook failed for %s", run_id)
