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
from backend.orchestrator.research import gather_sources, render_sources
from backend.orchestrator.state import dump_evidence
from backend.providers.base import ChatMessage, ProviderError

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
    task = state.get("task") or ""
    if state.get("command") == "/explain":
        task = f"Explain clearly, with examples from this workspace where relevant:\n{task}"
    context_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    web_note: str | None = None
    attachment_text = attachment_context(attachments)
    if attachment_text:
        context_parts.append(attachment_text)
    native_web_search = False
    if state.get("web_search"):
        try:
            await o.app.runtime.bindings_for(rc, config, task, state.get("category"), web_search=True)
            native_web_search = True
        except ProviderError as exc:
            if o.app.web_search.enabled and o.app.web_search.status()["configured"]:
                gathered, web_note = await gather_sources(rc, task, attachments, use_native=False)
                sources = [source.as_dict() for source in gathered]
                context_parts.append("Numbered sources (cite factual claims as [n]):\n" + render_sources(gathered))
                context_parts.append("NOTE: Using the configured Jevora Browser fallback because the selected "
                                     "model does not expose native web search.")
            else:
                web_note = f"Web browsing is not available for the selected model: {exc}"
                context_parts.append(f"NOTE: {web_note}")
    result = await o.app.runtime.run_agent(
        rc, config, task, role="chat", context="\n\n".join(context_parts), category=state.get("category"),
        stream=True, history=history, agent_id=state.get("agent_id"), include_images=bool(rc.image_parts),
        web_search=native_web_search)
    if native_web_search and result.status == "failed" and o.app.web_search.enabled \
            and o.app.web_search.status()["configured"]:
        # Provider-native search can be unavailable at runtime even when the
        # provider/model advertises it. Retry through the administrator-configured
        # Jevora Browser service instead of leaving the user with a hard failure.
        gathered, _ = await gather_sources(rc, task, attachments, use_native=False)
        if gathered:
            sources = [source.as_dict() for source in gathered]
            fallback_context = [*context_parts,
                                "Numbered sources (cite factual claims as [n]):\n" + render_sources(gathered),
                                "NOTE: Native provider web search failed, so Jevora Browser supplied these sources."]
            result = await o.app.runtime.run_agent(
                rc, config, task, role="chat", context="\n\n".join(fallback_context),
                category=state.get("category"), stream=True, history=history,
                agent_id=state.get("agent_id"), include_images=bool(rc.image_parts), web_search=False)
            web_note = "Native provider web search failed; Jevora Browser supplied the sources instead."
            native_web_search = False
    if native_web_search:
        sources = result.sources
        if not sources and result.status == "done":
            web_note = "The selected provider returned no web citations for this answer."
    return {"worker_output": result.output, "worker_status": result.status, "worker_error": result.error,
            "worker_call_id": result.call_id, "files_changed": result.files_changed,
            "evidence": dump_evidence(result.evidence), "sources": sources, "web_note": web_note,
            "worker_attempts": state.get("worker_attempts", 0) + 1}
