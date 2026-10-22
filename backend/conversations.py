"""Shared conversation ownership and retry-safe message persistence."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.errors import Conflict, NotFound
from backend.core.ids import utcnow
from backend.db.models import Conversation, Message


async def owned_conversation(session: AsyncSession, user_id: str, conversation_id: str,
                             mode: str | None = None) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise NotFound("conversation not found")
    if mode is not None and conversation.mode != mode:
        raise Conflict("conversation mode does not match run mode", code="conversation_mode_mismatch")
    return conversation


async def save_message(session: AsyncSession, conversation: Conversation, run_id: str,
                       role: str, content: str) -> None:
    """Upsert by run/role, including messages saved before deterministic IDs were introduced.

    A run has one executor; checkpoint replay can repeat finalization, but must update the
    same message. Deterministic primary keys also prevent duplicate inserts for new turns.
    The caller commits the whole turn and its conversation timestamp atomically.
    """
    message = await session.scalar(select(Message).where(
        Message.conversation_id == conversation.id, Message.run_id == run_id, Message.role == role)
        .order_by(Message.created_at, Message.id))
    if message is None:
        message_id = "msg_" + sha256(f"{run_id}:{role}".encode()).hexdigest()[:56]
        session.add(Message(id=message_id, conversation_id=conversation.id, run_id=run_id,
                            role=role, content=content))
    else:
        message.content = content
    conversation.updated_at = utcnow()
