"""Scheduled agent tasks: CRUD + run-now."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import NotFound
from backend.core.ids import utcnow
from backend.db.models import ScheduledTask

router = APIRouter(prefix="/api/scheduled", tags=["scheduled"])


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=100_000)
    mode: str = "chat"
    interval_minutes: int = Field(default=1440, ge=1, le=60 * 24 * 30)
    conversation_id: str | None = None
    workspace_id: str | None = None


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    interval_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    enabled: bool | None = None


def _public(t: ScheduledTask) -> dict[str, Any]:
    return {"id": t.id, "name": t.name, "prompt": t.prompt, "mode": t.mode,
            "interval_minutes": t.interval_minutes, "enabled": t.enabled,
            "conversation_id": t.conversation_id, "workspace_id": t.workspace_id,
            "last_run_id": t.last_run_id, "next_run_at": t.next_run_at.isoformat(),
            "created_at": t.created_at.isoformat()}


@router.get("")
async def list_tasks(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.sessions() as session:
        rows = list(await session.scalars(select(ScheduledTask).where(
            ScheduledTask.user_id == user.id).order_by(ScheduledTask.created_at.desc())))
    return [_public(t) for t in rows]


@router.post("", status_code=201)
async def create_task(body: TaskCreate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    task = ScheduledTask(user_id=user.id, name=body.name, prompt=body.prompt, mode=body.mode,
                         interval_minutes=body.interval_minutes,
                         conversation_id=body.conversation_id, workspace_id=body.workspace_id,
                         next_run_at=utcnow() + timedelta(minutes=body.interval_minutes))
    async with ctx.sessions() as session:
        session.add(task)
        await session.commit()
        await session.refresh(task)
    return _public(task)


async def _owned(session, user_id: str, task_id: str) -> ScheduledTask:
    task = await session.get(ScheduledTask, task_id)
    if task is None or task.user_id != user_id:
        raise NotFound("scheduled task not found")
    return task


@router.patch("/{task_id}")
async def update_task(task_id: str, body: TaskUpdate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        task = await _owned(session, user.id, task_id)
        if body.name is not None:
            task.name = body.name
        if body.prompt is not None:
            task.prompt = body.prompt
        if body.interval_minutes is not None:
            task.interval_minutes = body.interval_minutes
            task.next_run_at = utcnow() + timedelta(minutes=body.interval_minutes)
        if body.enabled is not None:
            task.enabled = body.enabled
            if body.enabled:
                task.next_run_at = utcnow() + timedelta(minutes=task.interval_minutes)
        await session.commit()
        await session.refresh(task)
        return _public(task)


@router.delete("/{task_id}")
async def delete_task(task_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        task = await _owned(session, user.id, task_id)
        await session.delete(task)
        await session.commit()
    return {"ok": True, "id": task_id}


@router.post("/{task_id}/run")
async def run_now(task_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        task = await _owned(session, user.id, task_id)
    run = await ctx.runs.create(user.id, text=task.prompt, mode=task.mode,
                                conversation_id=task.conversation_id, workspace_id=task.workspace_id,
                                meta={"scheduled_task_id": task.id})
    async with ctx.sessions() as session:
        row = await session.get(ScheduledTask, task_id)
        row.last_run_id = run.id
        row.next_run_at = utcnow() + timedelta(minutes=row.interval_minutes)
        await session.commit()
    return {"ok": True, "run_id": run.id}
