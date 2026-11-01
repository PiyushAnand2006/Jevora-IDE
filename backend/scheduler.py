"""Background scheduler: creates runs for due scheduled tasks.

One asyncio loop per app instance polls every 30 s for enabled tasks whose
``next_run_at`` is due, creates the run (reusing the task's conversation when
set, so follow-up runs keep context), and pushes ``next_run_at`` forward.
A task is skipped (postponed by one poll) while its conversation still has an
active run, so recurring tasks never stack on a busy conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.core.ids import utcnow
from backend.core.logging import get_logger
from backend.db.models import Run, ScheduledTask

if TYPE_CHECKING:
    from backend.app_context import AppContext

log = get_logger(__name__)
POLL_SECONDS = 30


class TaskScheduler:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="task-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await self.tick()
            except Exception:
                log.exception("scheduled-task tick failed")

    async def tick(self) -> int:
        """Create runs for due tasks; returns how many were dispatched."""
        now = utcnow()
        dispatched = 0
        async with self.app.sessions() as session:
            rows = list(await session.scalars(
                select(ScheduledTask).where(ScheduledTask.enabled.is_(True),
                                            ScheduledTask.next_run_at <= now)))
        for task in rows:
            if await self._conversation_busy(task):
                continue
            run = await self.app.runs.create(
                task.user_id, text=task.prompt, mode=task.mode,
                conversation_id=task.conversation_id, workspace_id=task.workspace_id,
                meta={"scheduled_task_id": task.id, "scheduled": True})
            async with self.app.sessions() as session:
                row = await session.get(ScheduledTask, task.id)
                if row is None:
                    continue
                row.last_run_id = run.id
                row.next_run_at = utcnow() + timedelta(minutes=row.interval_minutes)
                await session.commit()
            dispatched += 1
            log.info("scheduled task %s dispatched run %s", task.id, run.id)
        return dispatched

    async def _conversation_busy(self, task: ScheduledTask) -> bool:
        if not task.conversation_id:
            return False
        async with self.app.sessions() as session:
            active = await session.scalar(
                select(Run.id).where(Run.conversation_id == task.conversation_id,
                                     Run.status.in_(("queued", "planning", "running", "verifying", "awaiting_user"))))
        return active is not None
