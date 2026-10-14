"""planner node: heavy-reasoning plan with acceptance criteria and a test command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.agents.templates import template
from backend.orchestrator.nodes.common import attachment_context, ctx_for, load_attachments, set_status

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


async def planner(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "planning")
    attachments = await load_attachments(o, state)
    context = attachment_context(attachments)
    spec = state.get("spec") or {}
    if spec:
        context = (f"Task triage: {spec}\n\n" + context).strip()
    result = await o.app.runtime.run_agent(rc, template("planner"), state["task"], role="planner", context=context)
    plan = result.output if result.status == "done" else ""
    if plan:
        rc.recorder.emit("plan.created", {"plan": plan[:6000]}, call_id=result.call_id)
        await rc.recorder.add_artifact("spec", meta={"plan": plan[:20_000]}, call_id=result.call_id)
    return {"plan": plan}
