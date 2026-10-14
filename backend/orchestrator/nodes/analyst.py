"""analyst node: deterministic static analysis (evidence) + an analyst agent's structured report."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from backend.decision.schemas import EvidenceFactory, StaticAnalysisPayload
from backend.orchestrator.nodes.common import attachment_context, ctx_for, load_attachments, set_status, worker_config
from backend.orchestrator.state import dump_evidence
from backend.orchestrator.static_analysis import analyze_workspace, render_summary

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


async def analyst(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    report = await asyncio.to_thread(analyze_workspace, rc.workspace)
    payload = StaticAnalysisPayload(files=report["files"], lines=report["lines"], findings=report["findings"],
                                    summary={k: report[k] for k in ("languages", "todo_markers")} | {
                                        "avg_complexity": report["python"]["avg_complexity"]})
    attachments = await load_attachments(o, state)
    context = ["Static analysis of the workspace:\n" + render_summary(report),
               "Raw analysis data (JSON):\n" + json.dumps(report, default=str)[:20_000]]
    if attachments:
        context.append(attachment_context(attachments))
    if state.get("feedback"):
        context.append("The previous report was not accepted: " + state["feedback"])
    config = await worker_config(o, state, "analyst")
    result = await o.app.runtime.run_agent(rc, config, state["task"], role="analyst", context="\n\n".join(context),
                                           category="heavy_reasoning", stream=True, agent_id=state.get("agent_id"))
    rel = f".jevora/reports/{rc.run_id}.md"
    target = rc.workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# Analysis report\n\n## Task\n{state['task']}\n\n## Static analysis\n"
                      f"{render_summary(report)}\n\n## Findings\n{result.output}\n", encoding="utf-8")
    await rc.recorder.add_artifact("spec", path=rel, meta={"report": "analysis", "static": payload.model_dump()},
                                   call_id=result.call_id)
    evidence = [*result.evidence, EvidenceFactory.make("static_analysis", "static_analyzer", payload)]
    return {"worker_output": result.output, "worker_status": result.status, "worker_error": result.error,
            "worker_call_id": result.call_id, "evidence": dump_evidence(evidence), "analysis": report,
            "report_path": rel, "worker_attempts": state.get("worker_attempts", 0) + 1}
