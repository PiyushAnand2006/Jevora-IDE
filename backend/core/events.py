"""In-process event bus with replay (`last_seq`) for WebSocket/SSE streaming.

Every event carries a single, globally monotonic ``seq``. A run stream replays the
buffered events of that run with ``seq > last_seq`` and then follows live events, so a
client that reconnects with the last ``seq`` it saw misses nothing still in the buffer.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import threading
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from backend.core.ids import iso, utcnow
from backend.core.logging import get_logger
from backend.core.metrics import Metrics

log = get_logger(__name__)

EVENT_TYPES = frozenset({
    "run.status", "run.created", "run.deleted", "agent.started", "agent.finished", "agent.failed",
    "subagent.spawned", "token.delta", "token.reset", "tool.call", "tool.result", "file.changed",
    "terminal.output", "test.result", "artifact.created", "decision.made", "escalation.requested",
    "escalation.resolved", "vault.written", "agents.status", "plan.created", "error", "heartbeat",
})

TERMINAL_STATUSES = frozenset({"done", "failed", "escalated"})


@dataclass
class Event:
    type: str
    seq: int
    ts: str
    user_id: str | None = None
    run_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "seq": self.seq, "ts": self.ts, "run_id": self.run_id,
                "call_id": self.call_id, "parent_call_id": self.parent_call_id, "data": self.data}


class Subscription:
    def __init__(self, bus: EventBus, run_id: str | None, user_id: str | None, maxsize: int = 20000) -> None:
        self.bus = bus
        self.run_id = run_id
        self.user_id = user_id
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.lagged = False

    def matches(self, event: Event) -> bool:
        if self.run_id is not None:
            return event.run_id == self.run_id
        return self.user_id is None or event.user_id in (self.user_id, None)

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.lagged = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)

    def close(self) -> None:
        self.bus._unsubscribe(self)
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event


class EventBus:
    """In-memory bus with optional SQLite persistence for stream replay.

    Without persistence (tests, ephemeral use) behaviour is unchanged: replay
    comes from the in-memory ring buffers, which are lost on restart. When
    ``attach_persistence`` is called, every replay-worthy event is queued and a
    background task flushes it to the ``events`` table (batched, ~0.5 s), the
    seq counter continues from the persisted maximum on ``restore()``, and
    ``history_durable`` merges DB rows with the live buffer so a reconnecting
    client after a restart still gets a gap-free replay.
    """

    #: high-frequency events with no replay value after a restart
    PERSIST_SKIP = frozenset({"token.delta", "heartbeat", "token.reset"})

    def __init__(self, metrics: Metrics | None = None, run_buffer: int = 20000, global_buffer: int = 5000) -> None:
        self.metrics = metrics
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._run_buffers: dict[str, deque[Event]] = {}
        self._run_buffer_size = run_buffer
        self._global: deque[Event] = deque(maxlen=global_buffer)
        self._subs: list[Subscription] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        # persistence (attached via attach_persistence)
        self._sessions: Any | None = None
        self._pending: list[Event] = []
        self._flusher: asyncio.Task[None] | None = None
        self._restored = False

    # ------------------------------------------------------------------ lifecycle
    def attach_persistence(self, sessions: Any) -> None:
        """Enable durable replay against an async_sessionmaker (events table)."""
        self._sessions = sessions

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._sessions is not None and self._flusher is None and not loop.is_closed():
            self._flusher = loop.create_task(self._flush_loop(), name="event-flusher")

    async def restore(self) -> None:
        """Continue the global seq counter from the persisted maximum (call once at startup)."""
        if self._sessions is None or self._restored:
            return
        from sqlalchemy import func, select

        from backend.db.models import EventRow
        async with self._sessions() as session:
            max_seq = await session.scalar(select(func.max(EventRow.seq)))
        self._seq = itertools.count((max_seq or 0) + 1)
        self._restored = True
        log.info("event bus restored (next seq=%d)", (max_seq or 0) + 1)

    async def close(self) -> None:
        """Stop the flusher after a final drain."""
        task, self._flusher = self._flusher, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._flush()

    @property
    def last_seq(self) -> int:
        return self._global[-1].seq if self._global else 0

    def publish(self, type: str, *, user_id: str | None = None, run_id: str | None = None,
                call_id: str | None = None, parent_call_id: str | None = None,
                data: dict[str, Any] | None = None) -> Event:
        """Publish from the event-loop thread. Use ``publish_threadsafe`` from worker threads."""
        if type not in EVENT_TYPES:
            log.debug("publishing non-standard event type %s", type)
        with self._lock:
            event = Event(type=type, seq=next(self._seq), ts=iso(utcnow()) or "", user_id=user_id,
                          run_id=run_id, call_id=call_id, parent_call_id=parent_call_id, data=data or {})
            if run_id is not None:
                buffer = self._run_buffers.get(run_id)
                if buffer is None:
                    buffer = deque(maxlen=self._run_buffer_size)
                    self._run_buffers[run_id] = buffer
                buffer.append(event)
            if type != "token.delta":
                self._global.append(event)
            if self._sessions is not None and type not in self.PERSIST_SKIP:
                self._pending.append(event)
            subs = list(self._subs)
        for sub in subs:
            if sub.matches(event):
                sub.offer(event)
        if self.metrics is not None:
            self.metrics.events.labels(type).inc()
        return event

    def publish_threadsafe(self, type: str, **kwargs: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.publish(type, **kwargs)
        else:
            loop.call_soon_threadsafe(lambda: self.publish(type, **kwargs))

    # ------------------------------------------------------------------ persistence
    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            with contextlib.suppress(Exception):
                await self._flush()

    async def _flush(self) -> None:
        if self._sessions is None:
            return
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return
        from backend.db.models import EventRow
        try:
            async with self._sessions() as session:
                session.add_all(EventRow(
                    seq=e.seq, run_id=e.run_id, user_id=e.user_id, type=e.type,
                    call_id=e.call_id, parent_call_id=e.parent_call_id, ts=e.ts, data=e.data,
                ) for e in batch)
                await session.commit()
        except Exception:
            log.exception("event flush failed (%d events dropped)", len(batch))

    def history(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._run_buffers.get(run_id, ()) if e.seq > after_seq]

    async def history_durable(self, run_id: str, after_seq: int = 0) -> list[Event]:
        """Replay across restarts: merge DB rows with the live in-memory buffer."""
        mem = self.history(run_id, after_seq)
        if self._sessions is None:
            return mem
        from sqlalchemy import select

        from backend.db.models import EventRow
        try:
            async with self._sessions() as session:
                rows = await session.scalars(
                    select(EventRow).where(EventRow.run_id == run_id, EventRow.seq > after_seq)
                    .order_by(EventRow.seq))
                persisted = [Event(type=r.type, seq=r.seq, ts=r.ts, run_id=r.run_id, user_id=r.user_id,
                                   call_id=r.call_id, parent_call_id=r.parent_call_id, data=r.data)
                             for r in rows]
        except Exception:
            log.exception("event history load failed for run %s", run_id)
            return mem
        seen = {e.seq for e in mem}
        return sorted([e for e in persisted if e.seq not in seen] + mem, key=lambda e: e.seq)

    async def forget_run_persisted(self, run_id: str) -> None:
        """Drop a run's events from memory and the durable store (run deletion)."""
        self.forget_run(run_id)
        if self._sessions is None:
            return
        from sqlalchemy import delete

        from backend.db.models import EventRow
        try:
            async with self._sessions() as session:
                await session.execute(delete(EventRow).where(EventRow.run_id == run_id))
                await session.commit()
        except Exception:
            log.exception("event history delete failed for run %s", run_id)

    def global_history(self, user_id: str | None, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._global if e.seq > after_seq and (user_id is None or e.user_id in (user_id, None))]

    def subscribe(self, *, run_id: str | None = None, user_id: str | None = None) -> Subscription:
        sub = Subscription(self, run_id, user_id)
        with self._lock:
            self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def forget_run(self, run_id: str) -> None:
        with self._lock:
            self._run_buffers.pop(run_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


async def replay_then_follow(bus: EventBus, sub: Subscription, backlog: list[Event]) -> AsyncIterator[Event]:
    """Yield the replay backlog, then live events, de-duplicating by seq."""
    last = 0
    for event in backlog:
        last = max(last, event.seq)
        yield event
    async for event in sub:
        if event.seq <= last:
            continue
        last = event.seq
        yield event
