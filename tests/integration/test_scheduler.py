"""TaskScheduler tick: dispatch due tasks, conversation continuity, no stacking."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.core.ids import utcnow
from backend.db.models import Conversation, Run, ScheduledTask


async def _make_task(harness, user_id: str, *, prompt: str, conversation_id: str | None,
                     enabled: bool = True, interval_minutes: int = 60,
                     due_minutes_ago: int = 5) -> ScheduledTask:
    task = ScheduledTask(
        user_id=user_id, name=f"task {prompt}", prompt=prompt, mode="chat",
        interval_minutes=interval_minutes, enabled=enabled,
        conversation_id=conversation_id,
        next_run_at=utcnow() - timedelta(minutes=due_minutes_ago))
    async with harness.ctx.sessions() as session:
        session.add(task)
        await session.commit()
        await session.refresh(task)
    return task


async def _runs_for(harness, prompt: str) -> list[Run]:
    async with harness.ctx.sessions() as session:
        return list(await session.scalars(select(Run).where(Run.task == prompt)))


async def _get_task(harness, task_id: str) -> ScheduledTask:
    async with harness.ctx.sessions() as session:
        return await session.get(ScheduledTask, task_id)


@pytest.mark.asyncio
async def test_tick_dispatches_due_task_and_reuses_conversation(harness) -> None:
    me = (await harness.client.get("/api/auth/me")).json()
    uid = me["id"]
    async with harness.ctx.sessions() as session:
        conv = Conversation(user_id=uid, title="scheduled thread", mode="chat")
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
    task = await _make_task(harness, uid, prompt="weekly repo review",
                            conversation_id=conv.id, interval_minutes=60)

    dispatched = await harness.ctx.scheduler.tick()
    assert dispatched == 1

    runs = await _runs_for(harness, "weekly repo review")
    assert len(runs) == 1
    run = runs[0]
    assert run.conversation_id == conv.id          # continuity: same thread
    assert run.user_id == uid
    assert run.meta.get("scheduled_task_id") == task.id

    row = await _get_task(harness, task.id)
    assert row.last_run_id == run.id
    lead = (row.next_run_at - utcnow()).total_seconds()
    assert 60 * 60 - 30 <= lead <= 60 * 60 + 30    # pushed forward by one interval


@pytest.mark.asyncio
async def test_tick_defers_task_while_conversation_busy(harness) -> None:
    me = (await harness.client.get("/api/auth/me")).json()
    uid = me["id"]
    async with harness.ctx.sessions() as session:
        conv = Conversation(user_id=uid, title="busy thread", mode="chat")
        session.add(conv)
        await session.flush()
        session.add(Run(user_id=uid, mode="chat", task="long-running job",
                        conversation_id=conv.id, status="running"))
        await session.commit()
        await session.refresh(conv)
    task = await _make_task(harness, uid, prompt="hourly CVE check",
                            conversation_id=conv.id)
    due_before = task.next_run_at

    dispatched = await harness.ctx.scheduler.tick()
    assert dispatched == 0
    assert await _runs_for(harness, "hourly CVE check") == []

    row = await _get_task(harness, task.id)
    assert row.last_run_id is None
    assert row.next_run_at == due_before           # stays due; retried next poll


@pytest.mark.asyncio
async def test_tick_skips_disabled_and_dispatches_unbound(harness) -> None:
    me = (await harness.client.get("/api/auth/me")).json()
    uid = me["id"]
    paused = await _make_task(harness, uid, prompt="paused task",
                              conversation_id=None, enabled=False)
    await _make_task(harness, uid, prompt="fresh-thread task",
                     conversation_id=None)

    dispatched = await harness.ctx.scheduler.tick()
    assert dispatched == 1
    assert await _runs_for(harness, "paused task") == []

    runs = await _runs_for(harness, "fresh-thread task")
    assert len(runs) == 1
    assert runs[0].conversation_id is not None     # chat runs get a fresh thread
    assert (await _get_task(harness, paused.id)).last_run_id is None
