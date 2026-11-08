"""planner node: heavy-reasoning plan with acceptance criteria and a test command."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from backend.agents.templates import template
from backend.orchestrator.nodes.common import attachment_context, ctx_for, load_attachments, set_status

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

_ITEM_RE = re.compile(r"(?:\d+[.)]|[-*+])\s+(?:\[[ xX]\]\s*)?(.+)$")


def parse_plan_items(plan: str, limit: int = 12) -> list[str]:
    """Extract checklist items from the planner's markdown plan (numbered or bulleted lines)."""
    items: list[str] = []
    for line in (plan or "").splitlines():
        match = _ITEM_RE.match(line.strip())
        if match:
            text = match.group(1).strip()
            if text:
                items.append(text[:160])
        if len(items) >= limit:
            break
    if not items and (plan or "").strip():
        items = [plan.strip()[:160]]
    return items


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
    items: list[str] = []
    if plan:
        rc.recorder.emit("plan.created", {"plan": plan[:6000]}, call_id=result.call_id)
        await rc.recorder.add_artifact("spec", meta={"plan": plan[:20_000]}, call_id=result.call_id)
        # A live checklist for the run view (Antigravity-style); items tick when
        # the verifier confirms the work.
        items = parse_plan_items(plan)
        if items:
            rc.recorder.emit("task_list", {"items": items, "phase": "implementation"})
    return {"plan": plan, "plan_items": items}
