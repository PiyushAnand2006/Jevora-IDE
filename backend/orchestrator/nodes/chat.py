"""chat node: conversational agent with workspace awareness, light tools, streaming and memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

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

HISTORY_MESSAGES = 20


async def conversation_history(o: Orchestrator, conversation_id: str | None) -> list[ChatMessage]:
    if not conversation_id:
        return []
    async with o.app.sessions() as session:
        rows = list(await session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                                          .order_by(Message.created_at.desc()).limit(HISTORY_MESSAGES)))
    return [ChatMessage(role="assistant" if m.role == "assistant" else "user", content=m.content[:8000])
            for m in reversed(rows)]


async def chat(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    history = await conversation_history(o, state.get("conversation_id"))
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
