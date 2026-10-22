"""Owned conversation history, searchable cursor pagination, and titles."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import Ctx, CurrentUser
from backend.conversations import owned_conversation
from backend.core.errors import AppError
from backend.core.events import TERMINAL_STATUSES
from backend.core.ids import iso, utcnow
from backend.db.models import Conversation, Message, Run
from backend.orchestrator.runs import decode_cursor, encode_cursor

router = APIRouter(tags=["conversations"])


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=300)


def _run_public(run: Run) -> dict[str, Any]:
    return {"id": run.id, "status": run.status, "mode": run.mode, "task": run.task,
            "created_at": iso(run.created_at), "started_at": iso(run.started_at),
            "finished_at": iso(run.finished_at), "error": run.error, "cancelled": run.cancelled,
            "total_tokens": run.total_tokens, "final_output": run.final_output or ""}


async def _summaries(session: AsyncSession, conversations: list[Conversation]) -> list[dict[str, Any]]:
    ids = [c.id for c in conversations]
    counts: dict[str, int] = {}
    latest: dict[str, Run] = {}
    active: dict[str, list[str]] = {}
    if ids:
        counts = dict((await session.execute(select(Message.conversation_id, func.count(Message.id))
                      .where(Message.conversation_id.in_(ids)).group_by(Message.conversation_id))).all())
        for run in await session.scalars(select(Run).where(
                Run.conversation_id.in_(ids), Run.user_id == conversations[0].user_id)
                .order_by(Run.created_at.desc(), Run.id.desc())):
            assert run.conversation_id is not None
            latest.setdefault(run.conversation_id, run)
            if run.status not in TERMINAL_STATUSES:
                active.setdefault(run.conversation_id, []).append(run.id)
    return [{"id": c.id, "title": c.title, "mode": c.mode, "created_at": iso(c.created_at),
             "updated_at": iso(c.updated_at), "message_count": counts.get(c.id, 0),
             "latest_run": _run_public(latest[c.id]) if c.id in latest else None,
             "active_run_ids": active.get(c.id, [])} for c in conversations]


@router.get("/api/conversations")
async def list_conversations(user: CurrentUser, ctx: Ctx, limit: int = Query(20, ge=1, le=100),
                             cursor: str | None = None, q: str | None = None,
                             mode: str | None = None) -> dict[str, Any]:
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if mode:
        stmt = stmt.where(Conversation.mode == mode)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        matching_message = select(Message.id).where(
            Message.conversation_id == Conversation.id, Message.content.ilike(pattern, escape="\\")).exists()
        stmt = stmt.where(or_(Conversation.title.ilike(pattern, escape="\\"), matching_message))
    if cursor:
        try:
            updated_at, conversation_id = decode_cursor(cursor)
            stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise AppError("invalid cursor", code="invalid_cursor", status_code=422) from exc
        stmt = stmt.where(or_(Conversation.updated_at < stamp,
                              and_(Conversation.updated_at == stamp, Conversation.id < conversation_id)))
    stmt = stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
    async with ctx.sessions() as session:
        rows = list(await session.scalars(stmt))
        page = rows[:limit]
        items = await _summaries(session, page)
    next_cursor = encode_cursor(iso(page[-1].updated_at) or "", page[-1].id) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/api/conversations/{conversation_id}")
@router.get("/api/conversations/{conversation_id}/messages")
async def get_conversation(conversation_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        conversation = await owned_conversation(session, user.id, conversation_id)
        summary = (await _summaries(session, [conversation]))[0]
        messages = list(await session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                       .order_by(Message.created_at, Message.id)))
        runs = list(await session.scalars(select(Run).where(
            Run.conversation_id == conversation_id, Run.user_id == user.id).order_by(Run.created_at, Run.id)))
    return {**summary, "messages": [{"id": m.id, "conversation_id": m.conversation_id, "role": m.role,
                                    "content": m.content, "run_id": m.run_id, "created_at": iso(m.created_at)}
                                   for m in messages], "runs": [_run_public(r) for r in runs]}


@router.patch("/api/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdate,
                               user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    title = body.title.strip()
    if not title:
        raise AppError("title must not be empty", code="invalid_title", status_code=422)
    async with ctx.sessions() as session:
        conversation = await owned_conversation(session, user.id, conversation_id)
        conversation.title = title
        conversation.updated_at = utcnow()
        await session.commit()
        return (await _summaries(session, [conversation]))[0]
