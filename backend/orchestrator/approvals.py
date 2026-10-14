"""In-run approval waits (``awaiting_user``): a running agent blocks until the user answers."""

from __future__ import annotations

import asyncio
from typing import Any


class ApprovalBroker:
    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def register(self, escalation_id: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[escalation_id] = future
        return future

    def resolve(self, escalation_id: str, payload: dict[str, Any]) -> bool:
        future = self._waiters.pop(escalation_id, None)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    def discard(self, escalation_id: str) -> None:
        future = self._waiters.pop(escalation_id, None)
        if future is not None and not future.done():
            future.cancel()

    async def wait(self, escalation_id: str, timeout: float) -> dict[str, Any] | None:
        future = self._waiters.get(escalation_id) or self.register(escalation_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            self._waiters.pop(escalation_id, None)

    @property
    def pending(self) -> int:
        return len(self._waiters)
