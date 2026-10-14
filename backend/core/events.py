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
    def __init__(self, metrics: Metrics | None = None, run_buffer: int = 20000, global_buffer: int = 5000) -> None:
        self.metrics = metrics
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._run_buffers: dict[str, deque[Event]] = {}
        self._run_buffer_size = run_buffer
        self._global: deque[Event] = deque(maxlen=global_buffer)
        self._subs: list[Subscription] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

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

    def history(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._run_buffers.get(run_id, ()) if e.seq > after_seq]

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
