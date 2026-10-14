from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from backend.db.models import AgentCall, Decision
from tests.conftest import AppHarness, add_mock_provider, make_settings, start_app
from tests.integration.test_runs_api import wait_terminal
from tests.support.brain import Brain, Turn
from tests.support.scenarios import TEST_CALC, build_brain
from tests.support.servers import MockLLM, MockReply


async def calls(harness: Any, run_id: str) -> list[AgentCall]:
    async with harness.ctx.sessions() as session:
        return list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)
                                          .order_by(AgentCall.started_at)))


async def decisions(ctx: Any, run_id: str) -> list[Decision]:
    async with ctx.sessions() as session:
        return list(await session.scalars(select(Decision).where(Decision.run_id == run_id)
                                          .order_by(Decision.created_at)))


async def test_build_mode_happy_path_two_tiers(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain, _ = build_brain()
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py with add() and tests",
                                                           "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    assert run["status"] == "done", run["final_output"]
    last = run["decisions"][-1]
    assert last["type"] == "verification" and last["choice"] == "confirm_done" and last["confidence"] >= 0.65
    assert "2 passed" in run["final_output"] and "calc.py" in run["final_output"]
    rows = await calls(harness, run_id)
    roles = [r.role for r in rows]
    assert roles[:3] == ["router", "planner", "coder"] and "test-runner" in roles and "critic" in roles
    models = {r.role: r.model_used for r in rows if r.model_used}
    assert models["router"] == "mock-mini" and models["planner"] == "mock-pro"  # two distinct tiers
    types = {d.type for d in await decisions(harness.ctx, run_id)}
    assert {"routing", "assignment", "verification"} <= types
    diff = (await harness.client.get(f"/api/runs/{run_id}/diff")).json()
    assert "+def add(a, b):" in diff["diff"] and diff["stats"]["files_changed"] == 2
    tree = (await harness.client.get(f"/api/runs/{run_id}/tree")).json()
    coder_node = next(n for n in tree["roots"] if n["role"] == "coder")
    assert coder_node["decisions"][-1]["choice"] == "confirm_done"


async def test_wrong_output_claiming_success_is_not_confirmed(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    harness.ctx.settings.iteration_ceiling = 3
    brain, coder = build_brain(buggy=99)  # always buggy, always claims "All tests pass!"
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py with add()",
                                                           "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=90)
    verdicts = [d for d in await decisions(harness.ctx, run_id) if d.type == "verification" and d.acted]
    assert verdicts and all(d.choice != "confirm_done" for d in verdicts)
    assert {d.choice for d in verdicts} <= {"restart_same_agent", "reassign_new_agent", "escalate"}
    assert run["status"] == "escalated" and run["escalation"]["options"] == ["accept", "retry", "reassign", "abort"]
    assert run["decisions"][-1]["choice"] == "escalate"
    assert coder.attempts == 3  # iteration ceiling respected
    # the coder was given evidence-based feedback, not its own claim
    coder_prompts = [t for t in brain.turns if t.role == "coder" and t.step == 0]
    assert "tests failing" in json.dumps(coder_prompts[-1].messages).lower() or "failed" in coder_prompts[-1].task


async def test_escalation_answer_resumes_run(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    harness.ctx.settings.iteration_ceiling = 2
    brain, coder = build_brain(buggy=2)  # fails twice → escalate; the 3rd attempt (after "retry") succeeds
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=90)
    assert run["status"] == "escalated"
    answered = await harness.client.post(f"/api/runs/{run_id}/escalation",
                                         json={"choice": "retry", "message": "subtraction is wrong"})
    assert answered.status_code == 200
    await asyncio.sleep(0.2)
    run = await wait_terminal(harness.client, run_id, timeout=90)
    assert run["status"] == "done" and run["decisions"][-1]["choice"] == "confirm_done"
    assert coder.attempts == 3
    escalated = [d for d in await decisions(harness.ctx, run_id) if d.choice == "escalate" and d.acted]
    assert escalated[0].label == "restart_same_agent"  # the user's answer became labelled training data
    # The resumed coder saw the user's guidance.
    last_prompt = [t for t in brain.turns if t.role == "coder" and t.step == 0][-1]
    assert "subtraction is wrong" in json.dumps(last_prompt.messages)


async def test_escalation_accept_and_abort(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    harness.ctx.settings.iteration_ceiling = 1
    brain, _ = build_brain(buggy=99)
    brain.install(mock_llm)
    run_a = (await harness.client.post("/api/runs", json={"text": "task a", "mode": "build"})).json()["id"]
    run_b = (await harness.client.post("/api/runs", json={"text": "task b", "mode": "build"})).json()["id"]
    for run_id in (run_a, run_b):
        assert (await wait_terminal(harness.client, run_id, timeout=90))["status"] == "escalated"
    await harness.client.post(f"/api/runs/{run_a}/escalation", json={"choice": "accept"})
    await harness.client.post(f"/api/runs/{run_b}/escalation", json={"choice": "abort"})
    await asyncio.sleep(0.2)
    a = await wait_terminal(harness.client, run_a, timeout=30)
    b = await wait_terminal(harness.client, run_b, timeout=30)
    assert a["status"] == "done" and "accepted by the user" in a["final_output"]
    assert b["status"] == "failed" and "aborted" in (b["error"] or "")
    bad = await harness.client.post(f"/api/runs/{run_a}/escalation", json={"choice": "accept"})
    assert bad.status_code == 404


async def test_cancel_build_run_cancels_all_calls(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain, _ = build_brain(delay=30)
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "slow", "mode": "build"})).json()["id"]
    for _ in range(300):
        if any(r.role == "coder" for r in await calls(harness, run_id)):
            break
        await asyncio.sleep(0.05)
    cancelled = (await harness.client.post(f"/api/runs/{run_id}/cancel")).json()
    assert cancelled["status"] == "failed" and cancelled["cancelled"] is True
    rows = await calls(harness, run_id)
    assert all(r.status != "running" for r in rows)
    assert next(r for r in rows if r.role == "coder").status == "cancelled"
    assert harness.ctx.agent_pool.status() == {"agents_running": 0, "subagents_running": 0}


async def test_chat_mode_streams_and_remembers_conversation(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def chat(turn: Turn) -> MockReply | None:
        if turn.role == "chat":
            history = [m for m in turn.messages if m["role"] in ("user", "assistant")]
            return MockReply(content=f"Reply number {len(history) // 2 + 1} about your project.")
        return None

    Brain().on(chat).install(mock_llm)
    first = (await harness.client.post("/api/runs", json={"text": "hello there", "mode": "chat"})).json()
    run = await wait_terminal(harness.client, first["id"])
    assert run["status"] == "done" and run["final_output"] == "Reply number 1 about your project."
    assert run["conversation_id"]
    deltas = [e.data["text"] for e in harness.ctx.events.history(first["id"]) if e.type == "token.delta"]
    assert "".join(deltas) == "Reply number 1 about your project."
    second = (await harness.client.post("/api/runs", json={"text": "and again", "mode": "chat",
                                                           "conversation_id": run["conversation_id"]})).json()
    run2 = await wait_terminal(harness.client, second["id"])
    assert run2["final_output"] == "Reply number 2 about your project."
    assert run2["decisions"][-1]["type"] == "completion"
    assert not any(r.role == "router" for r in await calls(harness, second["id"]))  # chat skips triage


async def test_research_mode_cites_sources_without_web(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    (harness.ctx.settings.workspace_root / "notes.md").write_text(
        "Jevora uses LangGraph for orchestration and Laya for decisions.\n", encoding="utf-8")

    def research(turn: Turn) -> MockReply | None:
        if turn.role == "router":
            return MockReply(content='{"title":"t","intent":"research","needs_code_changes":false,'
                                     '"needs_tests":false,"ui_work":false,"keywords":[],"risk":"low"}')
        if turn.role == "researcher":
            assert "Web browsing is not available" in turn.task
            return MockReply(content="## Findings\nJevora orchestrates with LangGraph [1]. I could not browse "
                                     "the web in this session.")
        return None

    Brain().on(research).install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "What does Jevora use for orchestration?",
                                                           "mode": "general"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    assert run["status"] == "done" and run["mode"] == "research"
    assert run["decisions"][-1]["choice"] == "complete"
    assert "Sources:" in run["final_output"] and "notes.md" in run["final_output"]


async def test_analysis_mode_produces_report(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    ws = harness.ctx.settings.workspace_root
    (ws / "svc.py").write_text("import os\n\ndef f(x):\n    if x:\n        for i in range(x):\n            if i:\n"
                               "                pass\n    return x\n", encoding="utf-8")

    def analysis(turn: Turn) -> MockReply | None:
        if turn.role == "router":
            return MockReply(content='{"title":"t","intent":"analysis","needs_code_changes":false,'
                                     '"needs_tests":false,"ui_work":false,"keywords":[],"risk":"low"}')
        if turn.role == "analyst":
            assert "svc.py" in turn.task
            return MockReply(content="## Architecture\nOne module, svc.py; f has moderate complexity.")
        return None

    Brain().on(analysis).install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Analyse this repo", "mode": "analysis"})
              ).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    assert run["status"] == "done" and run["decisions"][-1]["choice"] == "complete"
    report = ws / ".jevora" / "reports" / f"{run_id}.md"
    assert report.exists() and "svc.py" in report.read_text(encoding="utf-8")


async def test_slash_commands(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain, _ = build_brain()
    brain.install(mock_llm)
    commands = (await harness.client.get("/api/commands")).json()
    assert {c["name"] for c in commands} >= {"/test", "/fix", "/design", "/research", "/explain", "/review",
                                            "/plan", "/run"}
    plan = (await harness.client.post("/api/runs", json={"text": "/plan build a calculator", "mode": "chat"})).json()
    assert plan["command"] == "/plan" and plan["mode"] == "build"
    plan_run = await wait_terminal(harness.client, plan["id"], timeout=60)
    assert plan_run["status"] == "done" and "Write calc.py" in plan_run["final_output"]
    assert not (harness.ctx.settings.workspace_root / "calc.py").exists()

    ran = (await harness.client.post("/api/runs", json={"text": "/run echo jevora-run-ok", "mode": "build"})).json()
    ran_run = await wait_terminal(harness.client, ran["id"])
    assert ran_run["status"] == "done" and "jevora-run-ok" in ran_run["final_output"]

    (harness.ctx.settings.workspace_root / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    (harness.ctx.settings.workspace_root / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                                                  encoding="utf-8")
    tested = (await harness.client.post("/api/runs", json={"text": "/test", "mode": "build"})).json()
    tested_run = await wait_terminal(harness.client, tested["id"], timeout=60)
    assert tested_run["status"] == "done" and "2 passed" in tested_run["final_output"]


async def test_plugin_toggles_change_behaviour(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    plugins = (await harness.client.get("/api/plugins")).json()
    assert {p["id"] for p in plugins} >= {"decision_verification", "iterative_tester", "dynamic_model_router"}
    for plugin in ("iterative_tester", "dynamic_model_router"):
        response = await harness.client.patch(f"/api/plugins/{plugin}", json={"enabled": False})
        assert response.status_code == 200 and response.json()["enabled"] is False
    brain, _ = build_brain()
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    rows = await calls(harness, run_id)
    assert "test-runner" not in [r.role for r in rows]  # no independent test pass
    assert len({r.model_used for r in rows if r.model_used}) == 1  # one default model for every agent
    assert run["status"] == "done"  # coder's own test evidence verified the work

    await harness.client.patch("/api/plugins/decision_verification", json={"enabled": False})
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py again", "mode": "build"})
              ).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    assert run["status"] == "done"
    assert all(d.type != "verification" for d in await decisions(harness.ctx, run_id))
    assert run["decisions"][-1]["type"] == "completion"


async def test_no_provider_fails_cleanly(harness: AppHarness) -> None:
    run_id = (await harness.client.post("/api/runs", json={"text": "build", "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id)
    assert run["status"] == "failed" and "No model provider is configured" in run["final_output"]


async def test_explicit_model_override_applies_to_agents(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain, _ = build_brain()
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build",
                                                           "model": "mock-mini"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=60)
    assert run["status"] == "done" and run["model"] == "mock-mini"
    assert {r.model_used for r in await calls(harness, run_id) if r.model_used} == {"mock-mini"}
    routing = [d for d in await decisions(harness.ctx, run_id) if d.type == "routing"]
    assert routing  # the routing decision is still logged


async def test_restart_mid_run_resumes_from_checkpoint(tmp_path: Path, mock_llm: MockLLM) -> None:
    settings = make_settings(tmp_path)
    app, lifespan = await start_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await add_mock_provider(client, mock_llm)
        brain, _coder = build_brain(delay=2.0)
        brain.install(mock_llm)
        run_id = (await client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
        for _ in range(400):
            async with app.state.ctx.sessions() as session:
                rows = list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)))
            if any(r.role == "coder" for r in rows):
                break
            await asyncio.sleep(0.02)
    await lifespan.__aexit__(None, None, None)  # simulated crash/restart while the coder is working
    planner_calls = sum(1 for t in brain.turns if t.role == "planner")
    assert planner_calls == 1

    app2, lifespan2 = await start_app(settings)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as client:
            run = await wait_terminal(client, run_id, timeout=90)
            assert run["status"] == "done", run["final_output"]
        assert sum(1 for t in brain.turns if t.role == "planner") == 1  # planner was NOT re-run
        async with app2.state.ctx.sessions() as session:
            rows = list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)))
        assert not any(r.status == "running" for r in rows)
        assert any(r.status == "cancelled" and r.role == "coder" for r in rows)  # the interrupted attempt
    finally:
        await lifespan2.__aexit__(None, None, None)

