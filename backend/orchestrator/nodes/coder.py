"""Build workers: coder, ui_designer and saved custom agents (same loop, different config)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.orchestrator.nodes.common import (
    attachment_context,
    ctx_for,
    image_parts,
    load_attachments,
    set_status,
    worker_config,
)
from backend.orchestrator.state import dump_evidence

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


def _task_text(state: GraphState) -> str:
    task = state["task"]
    command = state.get("command")
    if command == "/fix":
        return f"Fix the following problem. Reproduce it with a test first, then fix it:\n{task}"
    if command == "/design":
        return f"Design and implement this UI:\n{task}"
    return task


async def build_worker(o: Orchestrator, state: GraphState, template_name: str) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    config = await worker_config(o, state, template_name)
    attachments = await load_attachments(o, state)
    rc.image_parts = image_parts(attachments)
    context: list[str] = []
    if state.get("plan"):
        context.append("Implementation plan:\n" + state["plan"])
    if state.get("feedback"):
        context.append(f"Iteration {state.get('iteration', 0) + 1}: the previous attempt was NOT accepted. "
                       "Verification feedback from tool evidence:\n" + state["feedback"])
    if state.get("review"):
        context.append("Code review notes from the critic:\n" + state["review"])
    if attachments:
        context.append(attachment_context(attachments))
    category = config.category or ("creative_ui" if template_name == "ui-designer" else state.get("category"))
    result = await o.app.runtime.run_agent(
        rc, config, _task_text(state), role=config.template or config.name, context="\n\n".join(context),
        category=category, agent_id=state.get("agent_id"), include_images=bool(rc.image_parts))
    files = list(dict.fromkeys([*state.get("files_changed", []), *result.files_changed]))
    return {"worker_output": result.output, "worker_status": result.status, "worker_error": result.error,
            "worker_call_id": result.call_id, "files_changed": files, "evidence": dump_evidence(result.evidence),
            "worker_attempts": state.get("worker_attempts", 0) + 1}


async def coder(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    return await build_worker(o, state, "coder")


async def custom_agent(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    return await build_worker(o, state, "coder")
