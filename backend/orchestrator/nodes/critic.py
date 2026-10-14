"""critic node: lint evidence for changed files + one LLM code review per run (context, not evidence)."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from backend.agents.templates import template
from backend.agents.tools.terminal import git_in
from backend.agents.workspace_state import unified_diff
from backend.decision.schemas import EvidenceFactory, LintPayload
from backend.orchestrator.nodes.common import ctx_for, set_status
from backend.orchestrator.nodes.tester import system_tool
from backend.orchestrator.state import evidence_of

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState

_LINT_LINE = re.compile(r"^[^:\n]+:\d+:\d+: [A-Z]+\d+ ", re.M)


async def critic(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "verifying")
    evidence = list(state.get("evidence", []))
    updates: dict[str, Any] = {}
    review_mode = state.get("command") == "/review"

    py_files = [f for f in state.get("files_changed", []) if f.endswith(".py") and (rc.workspace / f).exists()]
    if py_files and not review_mode:
        files = " ".join(shlex.quote(f) for f in py_files[:50])
        command = f"python -m ruff check --select E9,F --output-format concise --no-cache {files}"
        result, _ = await system_tool(o, rc, role="linter", agent=template("tester"), tool="run_command",
                                      args={"command": command})
        output = result.content
        if "No module named ruff" not in output and "sandbox unavailable" not in output:
            errors = len(_LINT_LINE.findall(output))
            evidence.append(EvidenceFactory.make("lint", "sandbox", LintPayload(
                tool="ruff", errors=errors, exit_code=result.data.get("exit_code"),
                output_tail=output[-3000:])).model_dump(mode="json"))

    tests = evidence_of({**state, "evidence": evidence}).latest("test_result")
    tests_green = tests is not None and tests.failed == 0 and tests.errors == 0 and tests.exit_code == 0
    if review_mode or (not state.get("reviewed") and tests_green and state.get("files_changed")):
        if review_mode:
            code, diff_text = await git_in(rc.guard.root, "diff", "--no-color")
            if code != 0 or not diff_text.strip():
                diff_text = unified_diff(rc.snapshot)[0] if rc.snapshot else ""
            task = f"Review the current changes in this workspace. Focus: {state['task']}"
        else:
            diff_text = unified_diff(rc.snapshot)[0] if rc.snapshot else ""
            task = f"Review this change made for the task: {state['task']}"
        review = await o.app.runtime.run_agent(rc, template("critic"), task, role="critic",
                                               context=f"Diff:\n{diff_text[:40_000] or '(no diff)'}")
        updates["review"] = review.output
        updates["reviewed"] = True
        await rc.recorder.add_artifact("log", meta={"review": review.output[:20_000]}, call_id=review.call_id)
        if review_mode:
            updates["worker_output"] = review.output
            updates["worker_status"] = review.status
            updates["worker_call_id"] = review.call_id
    updates["evidence"] = evidence
    return updates

