"""Helpers shared by graph nodes."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from backend.agents.config import AgentConfig
from backend.agents.templates import template
from backend.db.models import Attachment
from backend.providers.base import ContentPart

if TYPE_CHECKING:
    from backend.agents.runtime import RunContext
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

MAX_ATTACHMENT_CONTEXT = 60_000


async def set_status(o: Orchestrator, state: GraphState, status: str, **fields: Any) -> None:
    rc = o.context(state["run_id"])
    await rc.recorder.set_status(status, **fields)


async def worker_config(o: Orchestrator, state: GraphState, template_name: str) -> AgentConfig:
    """The configured agent for this run: a saved custom agent, else the template."""
    if state.get("agent_id"):
        config = await o.app.agents.config(state["user_id"], str(state["agent_id"]))
    else:
        config = template(template_name)
    if state.get("stronger"):
        # Reassignment: same role, heavier model tier.
        binding = config.model_binding.model_copy(update={"mode": "auto", "category": "heavy_reasoning"})
        config = config.model_copy(update={"model_binding": binding})
    return config


async def load_attachments(o: Orchestrator, state: GraphState) -> list[dict[str, Any]]:
    ids = list(state.get("attachments") or [])
    if not ids:
        return []
    async with o.app.sessions() as session:
        rows = list(await session.scalars(select(Attachment).where(Attachment.id.in_(ids),
                                                                   Attachment.user_id == state["user_id"])))
    return [{"id": r.id, "filename": r.filename, "mime": r.mime, "kind": r.kind, "size": r.size,
             "path": r.storage_path, "text": r.text_content or ""} for r in rows]


def attachment_context(attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    budget = MAX_ATTACHMENT_CONTEXT
    for att in attachments:
        if att["kind"] == "image":
            parts.append(f"[image attached: {att['filename']} ({att['mime']}, {att['size']} bytes)]")
            continue
        text = att["text"][: max(0, budget)]
        budget -= len(text)
        if text:
            parts.append(f"--- attachment: {att['filename']} ---\n{text}")
        else:
            parts.append(f"[attachment {att['filename']} ({att['mime']}) has no extractable text]")
    return "\n\n".join(parts)


def image_parts(attachments: list[dict[str, Any]], limit_bytes: int = 8 * 1024 * 1024) -> list[ContentPart]:
    parts: list[ContentPart] = []
    for att in attachments:
        if att["kind"] != "image":
            continue
        path = Path(att["path"])
        if not path.exists() or path.stat().st_size > limit_bytes:
            continue
        parts.append(ContentPart(type="image", image_b64=base64.b64encode(path.read_bytes()).decode("ascii"),
                                 mime=att["mime"]))
    return parts


def ctx_for(o: Orchestrator, state: GraphState) -> RunContext:
    return o.context(state["run_id"])
