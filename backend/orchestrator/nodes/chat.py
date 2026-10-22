"""chat node: conversational agent with workspace awareness, light tools, streaming and memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from backend.conversations import owned_conversation
from backend.db.models import Message
from backend.orchestrator.nodes.common import (
    attachment_context,
    ctx_for,
    image_parts,
    load_attachments,
    set_status,
    worker_config,
)
from backend.orchestrator.state import dump_evidence
from backend.providers.base import ChatMessage

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

async def conversation_history(o: Orchestrator, conversation_id: str | None, user_id: str,
                               run_id: str) -> list[ChatMessage]:
    if not conversation_id:
        return []
    async with o.app.sessions() as session:
        await owned_conversation(session, user_id, conversation_id, "chat")
        rows = list(await session.scalars(select(Message).where(
            Message.conversation_id == conversation_id, or_(Message.run_id.is_(None), Message.run_id != run_id))
            .order_by(Message.created_at, Message.id)))
    # Projection happens after system/context/tool assembly in the runtime. Persisted
    # contents remain complete, even for messages larger than the current window.
    return [ChatMessage(role="assistant" if m.role == "assistant" else "user", content=m.content) for m in rows]


async def chat(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    history = await conversation_history(o, state.get("conversation_id"), rc.user_id, rc.run_id)
    attachments = await load_attachments(o, state)
    rc.image_parts = image_parts(attachments)
    config = await worker_config(o, state, "chat")
    task = state["task"]
    if state.get("command") == "/explain":
        task = f"Explain clearly, with examples from this workspace where relevant:\n{task}"
    result = await o.app.runtime.run_agent(
        rc, config, task, role="chat", context=attachment_context(attachments), category=state.get("category"),
        stream=True, history=history, agent_id=state.get("agent_id"), include_images=bool(rc.image_parts))
    return {"worker_output": result.output, "worker_status": result.status, "worker_error": result.error,
            "worker_call_id": result.call_id, "files_changed": result.files_changed,
            "evidence": dump_evidence(result.evidence), "worker_attempts": state.get("worker_attempts", 0) + 1}
