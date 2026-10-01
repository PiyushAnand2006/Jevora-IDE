from __future__ import annotations

import asyncio

import pytest

from backend import main
from backend.schemas import Decision, Evidence, TaskRun, TaskSpec, WorkVerdict
from tests.conftest import FakeProvider, StubDecisionEngine


async def passing_evidence(*_args, **_kwargs) -> Evidence:
    return Evidence(test_passed=True, tests_run=1, exec_log_tail="pytest: 1 passed", diff_summary="one file changed")


@pytest.mark.asyncio
async def test_restart_is_capped_then_escalated(monkeypatch: pytest.MonkeyPatch):
    engine = StubDecisionEngine([
        Decision(kind="work_verification", choice=WorkVerdict.restart_same_agent.value, confidence=0.9, engine="stub"),
        Decision(kind="work_verification", choice=WorkVerdict.restart_same_agent.value, confidence=0.9, engine="stub"),
    ])
    fake = FakeProvider([None, None, None])
    monkeypatch.setattr(main, "MAX_ITERATIONS", 2)
    monkeypatch.setattr(main, "get_decision_engine", lambda: engine)
    monkeypatch.setattr(main, "complete", fake.complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)

    run = TaskRun(spec=TaskSpec(text="Implement retry logic"))
    await main.run_agent(run)

    assert run.status == "escalated"
    assert len([agent for agent in run.agents if agent.config.template == "coder"]) == 2
    assert run.decisions[-1].choice == WorkVerdict.escalate.value
    assert "Retry limit of 2" in run.final_output


@pytest.mark.asyncio
async def test_low_confidence_escalates_instead_of_confirming(monkeypatch: pytest.MonkeyPatch):
    engine = StubDecisionEngine([Decision(kind="work_verification", choice=WorkVerdict.confirm_done.value,
                                          confidence=0.2, engine="stub", rationale="uncertain")])
    monkeypatch.setattr(main, "get_decision_engine", lambda: engine)
    monkeypatch.setattr(main, "complete", FakeProvider().complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)

    run = TaskRun(spec=TaskSpec(text="Small task"))
    await main.run_agent(run)

    assert run.status == "escalated"
    assert run.agents[-1].status == "escalated"


@pytest.mark.asyncio
async def test_subagent_events_record_the_actual_parent_id(monkeypatch: pytest.MonkeyPatch):
    engine = StubDecisionEngine()
    monkeypatch.setattr(main, "get_decision_engine", lambda: engine)
    monkeypatch.setattr(main, "complete", FakeProvider().complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)
    run = TaskRun(spec=TaskSpec(text="Build a feature"))

    await main.run_agent(run)

    agents = {agent.id: agent for agent in run.agents}
    calls = [event for event in main.EVENTS[run.id] if event["type"] == "subagent_called"]
    assert calls
    for event in calls:
        child = event["payload"]["agent"]
        assert child["parent_id"] == event["payload"]["parent_id"]
        assert child["parent_id"] in agents


@pytest.mark.asyncio
async def test_decision_engine_receives_tool_evidence_not_coder_self_report(monkeypatch: pytest.MonkeyPatch):
    engine = StubDecisionEngine()
    fake = FakeProvider(["plan", '{"summary":"I personally verified everything","files":[]}'])
    monkeypatch.setattr(main, "get_decision_engine", lambda: engine)
    monkeypatch.setattr(main, "complete", fake.complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)
    run = TaskRun(spec=TaskSpec(text="Build a feature"))

    await main.run_agent(run)

    evidence = engine.evidence[0]
    assert evidence.exec_log_tail == "pytest: 1 passed"
    assert "personally verified" not in evidence.model_dump_json()
    assert "I personally verified everything" not in evidence.model_dump_json()


@pytest.mark.asyncio
async def test_provider_timeout_finishes_in_clean_failed_state(monkeypatch: pytest.MonkeyPatch):
    async def timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(main, "get_decision_engine", lambda: StubDecisionEngine())
    monkeypatch.setattr(main, "complete", timeout)
    run = TaskRun(spec=TaskSpec(text="Build a feature"))

    await main.run_agent(run)

    assert run.status == "failed"
    assert "Run failed" in run.final_output
    assert main.EVENTS[run.id][-1]["type"] == "run_finished"


@pytest.mark.asyncio
async def test_tool_failure_reaches_clean_escalated_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "MAX_ITERATIONS", 1)
    monkeypatch.setattr(main, "get_decision_engine", lambda: __import__("backend.decision_engine", fromlist=["HeuristicDecisionEngine"]).HeuristicDecisionEngine())
    monkeypatch.setattr(main, "complete", FakeProvider().complete)

    async def failing_evidence(*_args, **_kwargs):
        return Evidence(test_passed=False, tests_run=1, exec_log_tail="tool failed")

    monkeypatch.setattr(main, "collect_test_evidence", failing_evidence)
    run = TaskRun(spec=TaskSpec(text="Build a feature"))
    await main.run_agent(run)

    assert run.status == "escalated"
    assert "Retry limit" in run.final_output


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [
    StubDecisionEngine(),
    StubDecisionEngine(route=Decision(kind="model_routing", choice="creative_ui", confidence=0.8, engine="alternate")),
])
async def test_orchestrator_accepts_swappable_decision_engines(monkeypatch: pytest.MonkeyPatch, engine):
    monkeypatch.setattr(main, "get_decision_engine", lambda: engine)
    monkeypatch.setattr(main, "complete", FakeProvider().complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)
    run = TaskRun(spec=TaskSpec(text="Build a feature"))

    await main.run_agent(run)

    assert run.status == "done"
    assert run.decisions[0].engine in {"stub", "alternate"}
