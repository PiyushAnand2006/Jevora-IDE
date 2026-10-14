from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents.config import AgentConfig
from backend.agents.pool import CallNode
from backend.agents.templates import template
from backend.agents.tools.base import ToolContext
from backend.decision.schemas import Evidence
from tests.conftest import AppHarness
from tests.support.runctx import make_run_context


async def tool_ctx(harness: AppHarness, config: AgentConfig | None = None) -> ToolContext:
    rc = await make_run_context(harness.ctx)
    config = config or template("coder")
    call_id = await rc.recorder.start_call(role="coder", agent_name=config.name, task="t")
    node = CallNode(call_id=call_id, config=config, key="k", depth=0)
    return ToolContext(app=harness.ctx, run_id=rc.run_id, user_id=rc.user_id, call_id=call_id, agent=config,
                       node=node, scope=rc.scope, recorder=rc.recorder, workspace=rc.workspace, guard=rc.guard,
                       sandbox_factory=rc.sandbox, extras={"run_context": rc, "runtime": harness.ctx.runtime})


async def invoke(harness: AppHarness, ctx: ToolContext, tool: str, **args: Any) -> Any:
    return await harness.ctx.tools.invoke(ctx, tool, args)


async def test_file_tools_round_trip(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    assert (await invoke(harness, ctx, "write_file", path="pkg/app.py", content="def f():\n    return 1\n")).ok
    read = await invoke(harness, ctx, "read_file", path="pkg/app.py")
    assert "return 1" in read.content
    edited = await invoke(harness, ctx, "edit_file", path="pkg/app.py", old_text="return 1", new_text="return 2")
    assert edited.ok and edited.files_changed == ["pkg/app.py"]
    patch = "--- a/pkg/app.py\n+++ b/pkg/app.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 2\n+    return 3\n"
    assert (await invoke(harness, ctx, "apply_patch", patch=patch)).ok
    assert (ctx.workspace / "pkg" / "app.py").read_text() == "def f():\n    return 3\n"
    listing = await invoke(harness, ctx, "list_dir", path=".")
    assert "app.py" in listing.content
    found = await invoke(harness, ctx, "search_files", pattern=r"return \d")
    assert "pkg/app.py:2" in found.content
    events = [e.type for e in harness.ctx.events.history(ctx.run_id)]
    assert events.count("file.changed") >= 3


async def test_tool_permission_and_path_checks(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness, template("critic"))  # read-only agent
    denied = await invoke(harness, ctx, "write_file", path="x.py", content="x")
    assert not denied.ok and "permission denied" in denied.content and "fs.write" in denied.content
    assert not (ctx.workspace / "x.py").exists()
    escape = await invoke(harness, ctx, "read_file", path="../../etc/passwd")
    assert not escape.ok and "outside the workspace" in escape.content
    coder = await tool_ctx(harness)
    git_write = await invoke(harness, coder, "write_file", path=".git/hooks/post-commit", content="evil")
    assert not git_write.ok and "not allowed" in git_write.content
    unknown = await invoke(harness, coder, "rm_everything")
    assert not unknown.ok and "unknown tool" in unknown.content
    missing = await invoke(harness, coder, "write_file", path="a.txt")
    assert not missing.ok and "missing required" in missing.content


async def test_read_binary_and_large_files(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    (ctx.workspace / "img.bin").write_bytes(b"\x89PNG\x00\x01\x02" * 10)
    result = await invoke(harness, ctx, "read_file", path="img.bin")
    assert result.ok and result.data["binary"] is True


async def test_run_command_and_blocked_commands(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    ok = await invoke(harness, ctx, "run_command", command="echo jevora-hello")
    assert ok.ok and "jevora-hello" in ok.content
    evidence = Evidence(items=ok.evidence)
    assert evidence.latest("execution_log").exit_code == 0
    blocked = await invoke(harness, ctx, "run_command", command="rm -rf /")
    assert not blocked.ok and blocked.content.startswith("blocked")
    failing = await invoke(harness, ctx, "run_command", command="exit 3")
    assert not failing.ok and failing.data["exit_code"] == 3
    created = await invoke(harness, ctx, "run_command", command="echo made > made_by_cmd.txt")
    assert created.ok
    changed = [e for e in harness.ctx.events.history(ctx.run_id) if e.type == "file.changed"]
    assert any(e.data["path"] == "made_by_cmd.txt" for e in changed)
    output = [e for e in harness.ctx.events.history(ctx.run_id) if e.type == "terminal.output"]
    assert any("jevora-hello" in e.data["data"] for e in output)


async def test_run_command_timeout(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    command = "ping -n 30 127.0.0.1 > NUL" if sys.platform == "win32" else "sleep 30"
    result = await invoke(harness, ctx, "run_command", command=command, timeout=1)
    assert not result.ok and result.data["timed_out"] is True


async def test_run_tests_produces_signed_evidence(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    ws = ctx.workspace
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
                                     "def test_bad():\n    assert add(1, 1) == 3\n", encoding="utf-8")
    result = await invoke(harness, ctx, "run_tests")
    assert not result.ok
    payload = Evidence(items=result.evidence).latest("test_result")
    assert payload.framework == "pytest" and payload.passed == 1 and payload.failed == 1
    assert any("test_bad" in f for f in payload.failures)
    (ws / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                                     encoding="utf-8")
    passed = await invoke(harness, ctx, "run_tests")
    assert passed.ok and Evidence(items=passed.evidence).latest("test_result").passed == 1
    events = [e for e in harness.ctx.events.history(ctx.run_id) if e.type == "test.result"]
    assert [e.data["ok"] for e in events] == [False, True]


async def test_run_tests_without_framework(harness: AppHarness) -> None:
    ctx = await tool_ctx(harness)
    result = await invoke(harness, ctx, "run_tests")
    assert not result.ok and "no test framework" in result.content


async def test_git_tools(harness: AppHarness) -> None:
    coder = template("coder")
    ctx = await tool_ctx(harness, coder.model_copy(update={"tool_permissions": [*coder.tool_permissions,
                                                                                "git.write"]}))
    (ctx.workspace / "readme.md").write_text("# hi\n", encoding="utf-8")
    commit = await invoke(harness, ctx, "git_commit", message="initial")
    assert commit.ok, commit.content
    (ctx.workspace / "readme.md").write_text("# hi\nmore\n", encoding="utf-8")
    diff = await invoke(harness, ctx, "git_diff")
    assert "+more" in diff.content
    status = await invoke(harness, ctx, "git_status")
    assert "readme.md" in status.content
    assert "initial" in (await invoke(harness, ctx, "git_log")).content
    bad = await invoke(harness, ctx, "git_branch", name="--upload-pack=evil")
    assert not bad.ok


async def test_risky_command_requires_approval(harness: AppHarness) -> None:
    harness.ctx.settings.approval_policy = "ask_on_risk"
    ctx = await tool_ctx(harness)
    (ctx.workspace / "build").mkdir()
    task = asyncio.create_task(invoke(harness, ctx, "run_command", command="rm -rf build"))
    for _ in range(100):
        run = (await harness.client.get(f"/api/runs/{ctx.run_id}")).json()
        if run["status"] == "awaiting_user":
            break
        await asyncio.sleep(0.02)
    assert run["status"] == "awaiting_user" and run["escalation"]["kind"] == "approval"
    assert run["escalation"]["options"] == ["approve", "deny"]
    answer = await harness.client.post(f"/api/runs/{ctx.run_id}/escalation", json={"choice": "deny"})
    assert answer.status_code == 200
    result = await asyncio.wait_for(task, timeout=10)
    assert not result.ok and "did not approve" in result.content
    assert (ctx.workspace / "build").exists()
    assert (await harness.client.get(f"/api/runs/{ctx.run_id}")).json()["status"] == "running"

    task = asyncio.create_task(invoke(harness, ctx, "run_command", command="rm -rf build"))
    for _ in range(100):
        if (await harness.client.get(f"/api/runs/{ctx.run_id}")).json()["status"] == "awaiting_user":
            break
        await asyncio.sleep(0.02)
    await harness.client.post(f"/api/runs/{ctx.run_id}/escalation", json={"choice": "approve"})
    result = await asyncio.wait_for(task, timeout=20)
    assert result.ok and not (ctx.workspace / "build").exists()


async def test_sandbox_unavailable_without_docker_or_flag(harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = harness.ctx.settings
    monkeypatch.setattr(settings, "sandbox_mode", "auto")
    monkeypatch.setattr(settings, "local_unsafe_exec", False)

    async def no_docker(refresh: bool = False) -> dict[str, Any]:
        return {"available": False, "version": None, "gvisor": False}

    monkeypatch.setattr(harness.ctx.sandboxes, "docker_info", no_docker)
    ctx = await tool_ctx(harness)
    result = await invoke(harness, ctx, "run_command", command="echo hi")
    assert not result.ok and "Docker is not available" in result.content
    health = (await harness.client.get("/api/system/health", params={"probe": "false"})).json()
    assert health["sandbox"]["available"] is False and "LOCAL_UNSAFE_EXEC" in health["sandbox"]["reason"]


def test_toolcontext_is_a_dataclass() -> None:
    assert dataclasses.is_dataclass(ToolContext)


def _unused(_: Path) -> None:  # keep Path import used for type checkers
    pass
