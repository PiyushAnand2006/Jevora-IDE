"""verifier node (Decision Engine on evidence only) and escalate node (pause for the user)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt
from sqlalchemy import select

from backend.db.models import EscalationRequest
from backend.decision.schemas import Evidence
from backend.orchestrator.nodes.common import ctx_for, set_status
from backend.orchestrator.state import evidence_of

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

ESCALATION_OPTIONS = ["accept", "retry", "reassign", "abort"]
_SECRET_FILE = re.compile(r"(^|/)(\.env|id_rsa|id_ed25519|.*\.pem|.*\.key|credentials)$", re.I)


def high_risk(evidence: Evidence) -> bool:
    diff = evidence.latest("diff")
    if diff is None:
        return False
    return (diff.deletions > 300 or len(diff.deleted_files) > 5
            or any(_SECRET_FILE.search(path) for path in diff.files))


def feedback_from(evidence: Evidence, reason: str) -> str:
    parts = [f"Decision: {reason}"]
    tests = evidence.latest("test_result")
    if tests is not None:
        parts.append(f"Test run `{tests.command}`: {tests.passed} passed, {tests.failed} failed, "
                     f"{tests.errors} errors (exit {tests.exit_code}).")
        if tests.failures:
            parts.append("Failing: " + "; ".join(tests.failures[:10]))
        if tests.failed or tests.errors or tests.exit_code:
            parts.append("Test output (tail):\n" + tests.output_tail[-3500:])
    elif evidence.latest("diff") is not None:
        parts.append("No test run was recorded. Add tests and run them with run_tests.")
    diff = evidence.latest("diff")
    if diff is not None and diff.files_changed == 0:
        parts.append("No files were changed in the workspace. Make the required changes with the file tools.")
    lint = evidence.latest("lint")
    if lint is not None and lint.errors:
        parts.append(f"Lint ({lint.tool}) reported {lint.errors} errors:\n{lint.output_tail[-1500:]}")
    screen = evidence.latest("screenshot")
    if screen is not None:
        failed = [f"{a.name}: {a.detail}" for a in screen.assertions if not a.passed]
        if failed:
            parts.append("Screen test assertions failed: " + "; ".join(failed))
        if screen.console_errors:
            parts.append("Browser console errors: " + "; ".join(screen.console_errors[:5]))
    parts.append("Fix the problems above. Your work is verified by re-running the tools, not by your summary.")
    return "\n".join(parts)


async def verifier(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "verifying")
    evidence = evidence_of(state)
    mode = state["mode"]
    iteration = int(state.get("iteration", 0))
    ceiling = int(state.get("max_iterations", o.app.settings.iteration_ceiling))
    decisions = o.app.decisions
    if mode in ("research", "analysis") or (mode == "chat" and not state.get("files_changed")):
        outcome = await decisions.confirm_completion(
            {"mode": mode, "has_output": bool((state.get("worker_output") or "").strip()), "iteration": iteration,
             "agent_status": state.get("worker_status")}, evidence, recorder=rc.recorder,
            call_id=state.get("worker_call_id"))
        result = outcome.result
        verification = {"type": "completion", "choice": result.choice, "confidence": result.confidence,
                        "engine": result.engine, "reason": result.reason, "decision_id": outcome.decision_id}
        if result.choice == "complete":
            return {"verification": verification, "escalation": None}
        if result.choice == "incomplete" and iteration + 1 < min(ceiling, 3):
            return {"verification": verification, "iteration": iteration + 1, "escalation": None,
                    "feedback": feedback_from(evidence, result.reason)}
        return {"verification": verification, "iteration": iteration + 1,
                "escalation": {"reason": f"output not confirmed complete: {result.reason}"}}

    tests_required = bool(state.get("requires_tests")) and (
        state.get("flags", {}).get("iterative_tester", True) or evidence.latest("test_result") is not None)
    features = {
        "mode": mode, "iteration": iteration, "max_iterations": ceiling,
        "same_agent_failures": int(state.get("same_agent_failures", 0)),
        "requires_tests": tests_required,
        "requires_changes": bool(state.get("requires_changes", mode == "build")),
        "agent_status": state.get("worker_status"), "high_risk": high_risk(evidence),
        "command": state.get("command"),
    }
    outcome = await decisions.verify_work(features, evidence, recorder=rc.recorder,
                                          call_id=state.get("worker_call_id"))
    result = outcome.result
    verification = {"type": "verification", "choice": result.choice, "confidence": result.confidence,
                    "engine": result.engine, "reason": result.reason, "decision_id": outcome.decision_id}
    await rc.recorder.update_run(iteration_count=iteration + 1)
    if result.choice == "confirm_done":
        return {"verification": verification, "escalation": None}
    updates: dict[str, Any] = {"verification": verification, "iteration": iteration + 1, "escalation": None,
                               "feedback": feedback_from(evidence, result.reason)}
    if result.choice == "restart_same_agent":
        updates["same_agent_failures"] = int(state.get("same_agent_failures", 0)) + 1
    elif result.choice == "reassign_new_agent":
        updates["same_agent_failures"] = 0
        updates["stronger"] = True
    if result.choice == "escalate" or iteration + 1 >= ceiling:
        updates["escalation"] = {"reason": result.reason if result.choice == "escalate" else
                                 f"iteration ceiling ({ceiling}) reached: {result.reason}"}
    return updates


async def escalate(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    escalation = dict(state.get("escalation") or {})
    verification = state.get("verification") or {}
    decision_id = verification.get("decision_id")
    reason = escalation.get("reason") or verification.get("reason") or "the system is not confident"
    async with o.app.sessions() as session:
        existing = await session.scalar(select(EscalationRequest).where(
            EscalationRequest.run_id == rc.run_id, EscalationRequest.kind == "decision",
            EscalationRequest.decision_id == decision_id).order_by(EscalationRequest.created_at.desc()))
    if existing is None:
        question = (f"Jevora could not confirm this task on its own: {reason}. Accept the current result, retry "
                    "with the same agent, reassign to a stronger model, or abort?")
        esc_id = await rc.recorder.open_escalation(
            question=question, options=ESCALATION_OPTIONS, kind="decision", decision_id=decision_id,
            call_id=state.get("worker_call_id"),
            context={"reason": reason, "iteration": state.get("iteration", 0), "verification": verification})
        summary = (state.get("worker_output") or "").strip()
        await set_status(o, state, "escalated", final_output=(
            f"Needs your decision: {reason}\n\n{summary}".strip()), iteration_count=state.get("iteration", 0))
    else:
        esc_id = existing.id
    answer: dict[str, Any] = interrupt({"escalation_id": esc_id, "reason": reason, "options": ESCALATION_OPTIONS})
    choice = str(answer.get("choice", "abort"))
    message = str(answer.get("message") or "")
    await set_status(o, state, "running")
    resolved = {**escalation, "id": esc_id, "choice": choice, "message": message, "reason": reason}
    iteration = int(state.get("iteration", 0))
    ceiling = int(state.get("max_iterations", o.app.settings.iteration_ceiling))
    if choice == "accept":
        return {"escalation": resolved, "final_status": "done"}
    if choice in ("retry", "reassign"):
        extra = f"\nUser guidance: {message}" if message else ""
        updates: dict[str, Any] = {"escalation": resolved, "max_iterations": max(ceiling, iteration + 2),
                                   "feedback": (state.get("feedback") or "") + extra}
        if choice == "reassign":
            updates.update({"stronger": True, "same_agent_failures": 0})
        return updates
    return {"escalation": resolved, "final_status": "failed", "error": "aborted by user after escalation"}
