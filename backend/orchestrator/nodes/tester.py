"""tester node: independent test pass (authoritative evidence) + workspace diff evidence.

Runs after every coding iteration when the Iterative Multi-Agent Tester plugin is on: writes tests
first if the project has none, then runs the suite itself in the sandbox. The coder's own claims
never substitute for this run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.agents.config import AgentConfig
from backend.agents.pool import CallNode
from backend.agents.templates import template
from backend.agents.tools.base import ToolContext, ToolResult
from backend.agents.tools.terminal import detect_test_command
from backend.agents.workspace_state import unified_diff
from backend.decision.schemas import DiffPayload, EvidenceFactory
from backend.orchestrator.nodes.common import ctx_for, set_status
from backend.orchestrator.state import dump_evidence

if TYPE_CHECKING:
    from backend.agents.runtime import RunContext
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


async def system_tool(o: Orchestrator, rc: RunContext, *, role: str, agent: AgentConfig, tool: str,
                      args: dict[str, Any], parent_call_id: str | None = None) -> tuple[ToolResult, str]:
    """Run a tool on behalf of the orchestrator itself, recorded as its own (model-less) call."""
    call_id = await rc.recorder.start_call(role=role, agent_name=role, task=f"{tool} {args}"[:2000],
                                           parent_call_id=parent_call_id, depth=1 if parent_call_id else 0)
    node = CallNode(call_id=call_id, config=agent, key=f"{role}:{tool}", depth=0)
    ctx = ToolContext(app=o.app, run_id=rc.run_id, user_id=rc.user_id, call_id=call_id, agent=agent, node=node,
                      scope=rc.scope, recorder=rc.recorder, workspace=rc.workspace, guard=rc.guard,
                      sandbox_factory=rc.sandbox, tool_call_id=call_id)
    o.app.agent_pool.enter(rc.scope, node)
    try:
        result = await o.app.tools.invoke(ctx, tool, args)
    finally:
        o.app.agent_pool.exit(rc.scope, node)
    await rc.recorder.finish_call(call_id, status="done" if result.ok else "failed", output=result.content[:20_000])
    return result, call_id


def diff_evidence(rc: RunContext) -> tuple[str, dict[str, Any], Any]:
    if rc.snapshot is None:
        return "", {"files_changed": 0, "additions": 0, "deletions": 0, "files": [], "deleted_files": []}, None
    diff, stats = unified_diff(rc.snapshot)
    payload = DiffPayload(files_changed=stats["files_changed"], additions=stats["additions"],
                          deletions=stats["deletions"], files=stats["files"][:200],
                          deleted_files=stats["deleted_files"][:200], diff_tail=diff[-4000:])
    return diff, stats, EvidenceFactory.make("diff", "workspace_diff", payload)


async def tester(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "verifying")
    evidence = list(state.get("evidence", []))
    updates: dict[str, Any] = {}
    flags = state.get("flags", {})
    run_tests_now = flags.get("iterative_tester", True) or state.get("command") == "/test"
    if run_tests_now:
        if (state.get("requires_tests") and detect_test_command(rc.workspace) is None
                and not state.get("tests_written")):
            files = ", ".join(state.get("files_changed", [])[:30]) or "(see workspace)"
            written = await o.app.runtime.run_agent(
                rc, template("tester"), f"Write automated tests (pytest for Python) that verify this task was "
                                        f"done correctly:\n{state['task']}",
                role="tester", context=f"Files changed by the coder: {files}\n\nPlan:\n{state.get('plan', '')}")
            evidence.extend(dump_evidence(written.evidence))
            updates["tests_written"] = True
            updates["files_changed"] = list(dict.fromkeys([*state.get("files_changed", []), *written.files_changed]))
        if detect_test_command(rc.workspace) is not None:
            result, _ = await system_tool(o, rc, role="test-runner", agent=template("tester"), tool="run_tests",
                                          args={})
            evidence.extend(dump_evidence(result.evidence))
        if state.get("command") == "/test" or flags.get("screen_test_requested"):
            screen = getattr(o.app, "screen", None)
            if screen is not None:
                screen_items = await screen.run_for_task(rc, state)
                evidence.extend(dump_evidence(screen_items))
    diff, stats, item = diff_evidence(rc)
    if item is not None:
        evidence.append(item.model_dump(mode="json"))
        if stats["files_changed"]:
            await rc.recorder.upsert_diff(diff, {k: v for k, v in stats.items() if k != "truncated"})
    updates["evidence"] = evidence
    return updates
