from __future__ import annotations

import asyncio

from sqlalchemy import select

from backend.agents.pool import RunBudget
from backend.agents.templates import template
from backend.db.models import AgentCall
from tests.conftest import AppHarness, add_mock_provider
from tests.support.brain import Brain, Turn, call, tools_reply
from tests.support.runctx import make_run_context
from tests.support.servers import MockLLM, MockReply


async def calls_for(harness: AppHarness, run_id: str) -> list[AgentCall]:
    async with harness.ctx.sessions() as session:
        return list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)
                                          .order_by(AgentCall.started_at)))


async def test_agent_tool_loop_writes_files(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def coder(turn: Turn) -> MockReply | None:
        if turn.role != "coder":
            return None
        if turn.step == 0:
            return tools_reply(call("write_file", path="hello.py", content="print('hello')\n"))
        return MockReply(content="Created hello.py")

    Brain().on(coder).install(mock_llm)
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "create hello.py")
    assert result.status == "done" and result.output == "Created hello.py"
    assert result.files_changed == ["hello.py"]
    assert (rc.workspace / "hello.py").read_text() == "print('hello')\n"
    rows = await calls_for(harness, rc.run_id)
    assert rows[0].status == "done" and rows[0].model_used == "mock-pro" and rows[0].tokens > 0
    types = [e.type for e in harness.ctx.events.history(rc.run_id)]
    assert {"agent.started", "tool.call", "tool.result", "agent.finished"} <= set(types)


async def test_subagent_lineage_and_narrowing(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def rules(turn: Turn) -> MockReply | None:
        if turn.role == "coder" and "parent task" in turn.task:
            if turn.step == 0:
                return tools_reply(call("spawn_subagent", template="ui-designer", task="design the page"),
                                   call("spawn_subagent", template="critic", task="review the change"))
            return MockReply(content="parent done")
        if turn.role == "ui-designer":
            if turn.step == 0:
                return tools_reply(call("spawn_subagent", template="critic", task="nested review"))
            return MockReply(content="designer done")
        if turn.role == "critic":
            names = {t["function"]["name"] for t in turn.body.get("tools", [])}
            assert "write_file" not in names  # narrowed + least privilege
            return MockReply(content="critic done")
        return None

    Brain().on(rules).install(mock_llm)
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "parent task")
    assert result.status == "done"
    rows = await calls_for(harness, rc.run_id)
    by_id = {r.id: r for r in rows}
    root = next(r for r in rows if r.parent_call_id is None)
    children = [r for r in rows if r.parent_call_id == root.id]
    assert sorted(c.role for c in children) == ["critic", "ui-designer"]
    designer = next(c for c in children if c.role == "ui-designer")
    nested = [r for r in rows if r.parent_call_id == designer.id]
    assert len(nested) == 1 and nested[0].depth == 2 and nested[0].lineage == [root.id, designer.id]
    assert all(by_id[r.parent_call_id].depth == r.depth - 1 for r in rows if r.parent_call_id)
    tree = (await harness.client.get(f"/api/runs/{rc.run_id}/tree")).json()
    assert tree["agents"] == 1 and tree["subagents"] == 3
    spawned = [e for e in harness.ctx.events.history(rc.run_id) if e.type == "subagent.spawned"]
    assert len(spawned) == 3 and all(e.parent_call_id for e in spawned)


async def test_depth_fanout_and_cycle_limits(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    harness.ctx.settings.max_subagent_depth = 2
    harness.ctx.settings.max_fanout = 2
    results: dict[str, list[str]] = {}

    def rules(turn: Turn) -> MockReply | None:
        if turn.role != "coder":
            return None
        if turn.step == 0 and "fanout" in turn.task:
            return tools_reply(*[call("spawn_subagent", template="tester", task=f"fanout child {i}")
                                 for i in range(3)])
        if turn.step == 0 and "cycle" in turn.task:
            return tools_reply(call("spawn_subagent", template="coder", task=turn.task))
        if turn.step == 0 and "deep" in turn.task:
            return tools_reply(call("spawn_subagent", template="coder", task=turn.task + " deeper"))
        results.setdefault(turn.task, []).extend(turn.tool_results())
        return MockReply(content="ok")

    Brain().on(rules).install(mock_llm)
    rc = await make_run_context(harness.ctx)
    await harness.ctx.runtime.run_agent(rc, template("coder"), "fanout test")
    fan = results["fanout test"]
    assert sum("refused: maximum fan-out" in r for r in fan) == 1

    await harness.ctx.runtime.run_agent(rc, template("coder"), "cycle test")
    assert any("cycle detected" in r for r in results["cycle test"])

    await harness.ctx.runtime.run_agent(rc, template("coder"), "deep")
    assert any("maximum subagent depth" in r for r in results["deep deeper deeper"])
    rows = await calls_for(harness, rc.run_id)
    assert max(r.depth for r in rows) == 2


async def test_cancel_propagates_to_descendants(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def rules(turn: Turn) -> MockReply | None:
        if turn.role == "coder" and turn.step == 0:
            return tools_reply(call("spawn_subagent", template="ui-designer", task="slow child"))
        if turn.role == "ui-designer" and turn.step == 0:
            return tools_reply(call("spawn_subagent", template="critic", task="slow grandchild"))
        if turn.role == "critic":
            return MockReply(content="never", delay=30)
        return MockReply(content="done")

    Brain().on(rules).install(mock_llm)
    rc = await make_run_context(harness.ctx)
    task = asyncio.create_task(harness.ctx.runtime.run_agent(rc, template("coder"), "root"))
    for _ in range(200):
        rows = await calls_for(harness, rc.run_id)
        if any(r.role == "critic" for r in rows):
            break
        await asyncio.sleep(0.02)
    assert harness.ctx.agent_pool.status()["subagents_running"] == 2
    cancelled = harness.ctx.agent_pool.cancel_run(rc.run_id)
    assert cancelled == 3
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.CancelledError:
        pass
    rows = await calls_for(harness, rc.run_id)
    assert {r.status for r in rows} == {"cancelled"} and len(rows) == 3
    assert harness.ctx.agent_pool.status() == {"agents_running": 0, "subagents_running": 0}


async def test_tool_call_loop_detection(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(tool_calls=[{"id": "c", "name": "list_dir", "arguments": {"path": "."}}]))
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "loop forever")
    assert result.status == "failed" and "tool-call loop detected" in (result.error or "")
    warned = [m for m in mock_llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert any("identical arguments" in str(m["content"]) for m in warned)


async def test_run_budget_enforced(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(tool_calls=[{"id": "c", "name": "list_dir", "arguments": {"depth": 1}}],
                               prompt_tokens=600, completion_tokens=600))
    rc = await make_run_context(harness.ctx, budget=RunBudget(max_tokens=1000, max_cost_usd=5, max_seconds=60))
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "spend tokens")
    assert result.status == "failed" and "token budget" in (result.error or "")


async def test_text_mode_fallback_for_models_without_tools(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def rules(turn: Turn) -> MockReply | None:
        if turn.has_tools:
            return MockReply(status=400)  # "does not support tools"
        if turn.step == 0:
            return MockReply(content='<tool_call>{"name": "write_file", "arguments": '
                                     '{"path": "t.txt", "content": "text mode"}}</tool_call>')
        return MockReply(content="finished in text mode")

    mock_llm.handler = Brain().on(rules)
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "use text tools")
    assert result.status == "done" and (rc.workspace / "t.txt").read_text() == "text mode"


async def test_inline_json_tool_call_from_local_models(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies('{"name": "write_file", "parameters": {"path": "j.txt", "content": "inline"}}', "done")
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.runtime.run_agent(rc, template("coder"), "inline")
    assert result.status == "done" and (rc.workspace / "j.txt").read_text() == "inline"


async def test_agent_memory_scope_persists(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies("the answer is 42")
    rc = await make_run_context(harness.ctx)
    chat = template("chat")
    await harness.ctx.runtime.run_agent(rc, chat, "what is the answer?")
    rc2 = await make_run_context(harness.ctx)
    await harness.ctx.runtime.run_agent(rc2, chat, "remind me")
    system = mock_llm.requests[-1]["messages"][0]["content"]
    assert "Notes from your previous runs" in system and "the answer is 42" in system


async def test_workspace_memory_retrieval(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    ws = harness.ctx.settings.workspace_root
    (ws / "billing.py").write_text("def compute_invoice_total(items):\n    return sum(items)\n", encoding="utf-8")
    (ws / "other.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")
    snippets = await harness.ctx.memory.search_workspace(harness.ctx.local_user_id, ws, "invoice total")
    assert snippets == []  # not indexed yet
    await harness.ctx.memory.index_workspace(harness.ctx.local_user_id, ws)
    snippets = await harness.ctx.memory.search_workspace(harness.ctx.local_user_id, ws, "invoice total")
    assert snippets and snippets[0].source.startswith("file:billing.py")
