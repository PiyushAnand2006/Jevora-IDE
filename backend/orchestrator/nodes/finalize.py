"""finalize node (status, output, diff, memory, conversation) and the /run command node."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.agents.templates import template
from backend.conversations import owned_conversation, save_message
from backend.core.logging import get_logger
from backend.db.models import Conversation, Run
from backend.decision.schemas import Evidence
from backend.orchestrator.nodes.common import ctx_for, set_status
from backend.orchestrator.nodes.researcher import sources_footer
from backend.orchestrator.nodes.tester import diff_evidence, system_tool
from backend.orchestrator.state import dump_evidence, evidence_of

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

log = get_logger(__name__)
SUCCESS = {"confirm_done", "complete"}


async def run_command(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "running")
    command = (state.get("command_arg") or state["task"]).strip()
    agent = template("coder")
    result, call_id = await system_tool(o, rc, role="terminal", agent=agent, tool="run_command",
                                        args={"command": command})

    return {"worker_output": result.content, "worker_status": "done" if result.ok else "failed",
            "worker_call_id": call_id, "evidence": dump_evidence(result.evidence),
            "worker_error": None if result.ok else result.content[:500]}


def _final_status(state: GraphState) -> str:
    if state.get("final_status"):
        return str(state["final_status"])
    if state.get("command") == "/plan":
        return "done" if (state.get("plan") or "").strip() else "failed"
    verification = state.get("verification") or {}
    if verification.get("choice") in SUCCESS:
        return "done"
    worker_ok = state.get("worker_status") == "done"
    flags = state.get("flags", {})
    if worker_ok and not flags.get("decision_verification", True):
        return "done"
    if worker_ok and state.get("command") in ("/plan", "/review", "/run"):
        return "done"
    if worker_ok and state["mode"] == "chat" and not verification:
        return "done"
    return "failed"


def _footer(state: GraphState, evidence: Evidence, diff_stats: dict[str, Any]) -> str:
    lines: list[str] = []
    verification = state.get("verification") or {}
    if verification:
        lines.append(f"Decision engine: {verification.get('choice')} ({float(verification.get('confidence', 0)):.0%}, "
                     f"{verification.get('engine')}) — {verification.get('reason', '')}".rstrip(" —"))
    tests = evidence.latest("test_result")
    if tests is not None:
        lines.append(f"Tests: {tests.passed} passed, {tests.failed} failed, {tests.errors} errors "
                     f"(`{tests.command}`)")
    screen = evidence.latest("screenshot")
    if screen is not None:
        ok = sum(1 for a in screen.assertions if a.passed)
        lines.append(f"Screen test: {ok}/{len(screen.assertions)} assertions passed, "
                     f"{len(screen.screenshots)} screenshots")
    if diff_stats.get("files_changed"):
        files = ", ".join(diff_stats["files"][:12]) + (" …" if len(diff_stats["files"]) > 12 else "")
        lines.append(f"Files changed ({diff_stats['files_changed']}, +{diff_stats['additions']}/"
                     f"-{diff_stats['deletions']}): {files}")
    if state.get("iteration"):
        lines.append(f"Iterations: {state.get('iteration')}")
    escalation = state.get("escalation") or {}
    if escalation.get("choice") == "accept":
        lines.append("Result accepted by the user after escalation.")
    return "\n".join(lines)


async def finalize(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    status = _final_status(state)
    evidence = evidence_of(state)
    diff, stats, _ = diff_evidence(rc)
    if stats.get("files_changed"):
        await rc.recorder.upsert_diff(diff, {k: v for k, v in stats.items() if k != "truncated"})
    flags = state.get("flags", {})
    verification = state.get("verification") or {}
    produced = (state.get("worker_output") or state.get("plan") or "").strip()
    if not verification and (state.get("worker_status") or produced) and not state.get("error"):
        # No verification loop ran (plugin off, chat, or a command): still log a completion verdict.
        outcome = await o.app.decisions.confirm_completion(
            {"mode": "chat" if state.get("command") in ("/plan", "/review", "/run") else state["mode"],
             "has_output": bool(produced),
             "verification_loop": flags.get("decision_verification", True)}, evidence,
            recorder=rc.recorder, call_id=state.get("worker_call_id"))
        verification = {"type": "completion", "choice": outcome.result.choice,
                        "confidence": outcome.result.confidence, "engine": outcome.result.engine,
                        "reason": outcome.result.reason}

    if state.get("error") and not state.get("worker_output"):
        output = f"Run failed: {state['error']}"
    else:
        body = (state.get("worker_output") or state.get("plan") or "").strip()
        if state.get("command") == "/plan" and state.get("plan"):
            body = state["plan"].strip()
        if state["mode"] in ("research", "chat") and (state.get("sources") or state.get("web_note")):
            note = state.get("web_note")
            if note and "web" not in body.lower()[:4000]:
                body += f"\n\n_{note}_"
            if state.get("sources"):
                body += sources_footer(state.get("sources") or [])
        if state.get("report_path"):
            body += f"\n\nFull report saved to `{state['report_path']}`."
        footer = _footer({**state, "verification": verification}, evidence, stats)
        output = f"{body}\n\n---\n{footer}" if footer and state["mode"] != "chat" else body
        if status == "failed" and state.get("error"):
            output = f"{output}\n\nRun failed: {state['error']}"
        elif status == "failed" and state.get("worker_error"):
            output = f"{output}\n\nRun failed: {state['worker_error']}"
    await _save_conversation(o, state, output)
    async with o.app.sessions() as session:
        run = await session.get(Run, rc.run_id)
        if run is not None:
            meta = dict(run.meta or {})
            if state.get("sources"):
                meta["sources"] = list(state["sources"])[:50]
            if state.get("web_note"):
                meta["web_note"] = state["web_note"]
            if state.get("report_path"):
                meta["report_path"] = state["report_path"]
            run.meta = meta
            await session.commit()
    if status == "done" and state["mode"] in ("build", "analysis", "research"):
        try:
            await o.app.memory.remember_workspace(rc.user_id, rc.workspace, f"run:{rc.run_id}",
                                                  f"Task: {state['task'][:500]}\nOutcome: {output[:1500]}")
        except Exception:  # memory is best-effort
            log.warning("could not store run summary in workspace memory", exc_info=True)
    await set_status(o, state, status, final_output=output[:200_000], iteration_count=int(state.get("iteration", 0)),
                     error=state.get("error") or (state.get("worker_error") if status == "failed" else None))
    return {"final_status": status, "final_output": output}


async def _save_conversation(o: Orchestrator, state: GraphState, output: str) -> None:
    if state["mode"] != "chat":
        return
    rc = ctx_for(o, state)
    async with o.app.sessions() as session:
        run = await session.get(Run, rc.run_id)
        conversation_id = state.get("conversation_id") or (run.conversation_id if run else None)
        if conversation_id:
            conversation = await owned_conversation(session, rc.user_id, conversation_id, "chat")
        else:
            # Compatibility for pre-conversation runs/checkpoints. New runs allocate
            # their conversation before scheduling; retries reuse the ID on the run.
            conversation = Conversation(user_id=rc.user_id, title=state["task"][:120], mode="chat")
            session.add(conversation)
            await session.flush()
        await save_message(session, conversation, rc.run_id, "user", state["task"])
        await save_message(session, conversation, rc.run_id, "assistant", output)
        if run is not None:
            run.conversation_id = conversation.id
        await session.commit()
