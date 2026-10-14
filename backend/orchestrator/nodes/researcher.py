"""researcher node: retrieval over attachments + workspace (+ web when enabled), cited report."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.decision.schemas import EvidenceFactory
from backend.orchestrator.nodes.common import ctx_for, image_parts, load_attachments, set_status, worker_config
from backend.orchestrator.research import Source, check_citations, gather_sources, render_sources
from backend.orchestrator.state import dump_evidence

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


async def researcher(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    attachments = await load_attachments(o, state)
    rc.image_parts = image_parts(attachments)
    sources, web_note = await gather_sources(rc, state["task"], attachments)
    context = ["Numbered sources (cite them as [n]):\n" + render_sources(sources)]
    if web_note:
        context.append(f"NOTE: {web_note} State this limitation briefly in the report.")
    if state.get("feedback"):
        context.append("The previous report was not accepted: " + state["feedback"])
    config = await worker_config(o, state, "researcher")
    result = await o.app.runtime.run_agent(rc, config, state["task"], role="researcher",
                                           context="\n\n".join(context), category="research_retrieval",
                                           stream=True, agent_id=state.get("agent_id"),
                                           include_images=bool(rc.image_parts))
    citations = check_citations(result.output, sources)
    evidence = [*result.evidence, EvidenceFactory.make("citations", "retrieval", citations)]
    await rc.recorder.add_artifact("spec", meta={"sources": [s.as_dict() for s in sources][:50],
                                                 "citations": citations.model_dump()}, call_id=result.call_id)
    return {"worker_output": result.output, "worker_status": result.status, "worker_error": result.error,
            "worker_call_id": result.call_id, "evidence": dump_evidence(evidence),
            "sources": [s.as_dict() for s in sources], "web_note": web_note,
            "worker_attempts": state.get("worker_attempts", 0) + 1}


def sources_footer(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return ""
    lines = [f"[{i}] {Source(**s).title} — {s['location']}" for i, s in enumerate(sources, 1)]
    return "\n\nSources:\n" + "\n".join(lines)
