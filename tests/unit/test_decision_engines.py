from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from backend.db.models import Decision
from backend.db.session import make_engine, make_sessionmaker, run_migrations
from backend.decision.ensemble import DecisionService
from backend.decision.eval.dataset import DatasetItem, export_logged, load_jsonl, seed_dataset
from backend.decision.eval.runner import GateCriteria, run_eval
from backend.decision.interface import DecisionEngine
from backend.decision.jev_engine import JevEngine
from backend.decision.laya_engine import LayaEngine
from backend.decision.laya_mock import TrainedLayaMock, ZeroShotLayaMock
from backend.decision.rules_engine import RulesEngine
from backend.decision.schemas import (
    SCHEMA_CHOICES,
    AgentNarrative,
    DecisionQuestion,
    DiffPayload,
    Evidence,
    EvidenceFactory,
    EvidenceItem,
    ScreenshotPayload,
    TestResultPayload,
)


def result_ev(passed: int, failed: int = 0, exit_code: int | None = None) -> EvidenceItem:
    code = exit_code if exit_code is not None else (1 if failed else 0)
    return EvidenceFactory.make("test_result", "sandbox", TestResultPayload(
        framework="pytest", command="pytest", exit_code=code, passed=passed, failed=failed))


def diff_ev(files: int) -> EvidenceItem:
    return EvidenceFactory.make("diff", "workspace_diff", DiffPayload(files_changed=files, additions=files * 5))


BASE = {"mode": "build", "iteration": 0, "max_iterations": 6, "requires_tests": True, "requires_changes": True,
        "same_agent_failures": 0, "agent_status": "done"}


# --------------------------------------------------------------------------- evidence
def test_evidence_rejects_agent_self_reports() -> None:
    for bad in ["All tests pass, trust me", AgentNarrative(text="done"), {"self_report": "ok"},
                {"items": ["tests passed"]}, {"items": [AgentNarrative(text="x")]}, {"claim": "fixed"}]:
        with pytest.raises(ValueError):
            Evidence.model_validate(bad)
    forged = result_ev(5).model_dump()
    forged["payload"]["passed"] = 999  # tampering invalidates the signature
    with pytest.raises(ValueError):
        EvidenceItem.model_validate(forged)
    with pytest.raises(ValueError):
        EvidenceItem(kind="test_result", source="sandbox", payload={"framework": "pytest", "command": "x",
                                                                    "exit_code": 0, "passed": 1}, signature="0" * 64)
    smuggled = EvidenceItem.model_construct(kind="test_result", source="sandbox", payload=forged["payload"],
                                            signature=forged["signature"], tool_call_id=None)
    with pytest.raises(ValueError):
        Evidence(items=[smuggled])


async def test_verify_work_requires_evidence_type() -> None:
    with pytest.raises(TypeError):
        await RulesEngine().verify_work(BASE, "tests passed")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- rules engine
@pytest.mark.parametrize("state,items,expected", [
    (BASE, [result_ev(3), diff_ev(2)], "confirm_done"),
    (BASE, [result_ev(1, failed=2), diff_ev(2)], "restart_same_agent"),
    ({**BASE, "same_agent_failures": 2}, [result_ev(1, failed=2), diff_ev(1)], "reassign_new_agent"),
    ({**BASE, "iteration": 5}, [result_ev(1, failed=1), diff_ev(1)], "escalate"),
    (BASE, [result_ev(3), diff_ev(0)], "restart_same_agent"),
    (BASE, [diff_ev(2)], "restart_same_agent"),
    ({**BASE, "agent_status": "failed"}, [result_ev(3), diff_ev(2)], "restart_same_agent"),
])
async def test_rules_verification(state: dict[str, Any], items: list[EvidenceItem], expected: str) -> None:
    result = await RulesEngine().verify_work(state, Evidence(items=items))
    assert result.choice == expected and result.engine == "rules"
    assert abs(sum(result.scores_per_option.values()) - 1.0) < 0.01


async def test_rules_screen_evidence() -> None:
    failing = EvidenceFactory.make("screenshot", "screen_driver", ScreenshotPayload(
        screenshots=[{"path": "a.png", "sha256": "x"}], assertions=[{"name": "title", "passed": False}]))
    result = await RulesEngine().verify_work({**BASE, "requires_tests": False}, Evidence(items=[failing, diff_ev(1)]))
    assert result.choice == "restart_same_agent" and "screen test failed" in result.reason


@pytest.mark.parametrize("task,mode,expected", [
    ("Implement a REST endpoint with pytest tests", "build", "code_generation"),
    ("Classify this ticket: yes or no", "chat", "fast_classification"),
    ("Design a responsive landing page with CSS", "build", "creative_ui"),
    ("Research and compare databases, cite sources", "research", "research_retrieval"),
    ("Plan the architecture and analyze bottlenecks", "analysis", "heavy_reasoning"),
])
async def test_rules_routing(task: str, mode: str, expected: str) -> None:
    result = await RulesEngine().route_model({"task": task, "mode": mode})
    assert result.choice == expected


# --------------------------------------------------------------------------- interface conformance
def _engines(client: httpx.AsyncClient) -> list[DecisionEngine]:
    def jev_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"ok": True})
        body = json.loads(request.content)
        options = list(body["questions"]["decision"]["criteria"])
        return httpx.Response(200, json={"answers": {"decision": {
            "choice": options[0], "confidence": 0.8, "probabilities": {o: 0.8 if i == 0 else 0.2 / (len(options) - 1)
                                                                        for i, o in enumerate(options)}}}})

    jev = JevEngine("https://jev.test", "key", httpx.AsyncClient(transport=httpx.MockTransport(jev_handler)))
    return [RulesEngine(), LayaEngine(model_id="mock", backend_factory=TrainedLayaMock),
            LayaEngine(model_id="mock", backend_factory=ZeroShotLayaMock), jev]


async def test_engines_conform_to_interface() -> None:
    async with httpx.AsyncClient() as client:
        evidence = Evidence(items=[result_ev(2), diff_ev(1)])
        for engine in _engines(client):
            assert await engine.available()
            results = [await engine.verify_work(BASE, evidence), await engine.route_model({"task": "build an api"}),
                       await engine.select_next_action({"mode": "build"}), await engine.assign_agent({"mode": "chat"}),
                       await engine.decide_retry({"error_kind": "provider_error"}),
                       await engine.confirm_completion({"mode": "chat", "has_output": True}, evidence)]
            for result in results:
                assert result.choice in SCHEMA_CHOICES[result.type]
                assert 0.0 <= result.confidence <= 1.0 and result.engine == engine.name
                assert set(result.scores_per_option) <= set(SCHEMA_CHOICES[result.type])
                assert result.latency_ms >= 0


async def test_laya_is_lazy_batched_and_handles_missing_weights() -> None:
    backend = TrainedLayaMock()
    engine = LayaEngine(model_id="mock", backend_factory=lambda: backend, batch_window_s=0.02)
    assert not engine.loaded
    questions = [DecisionQuestion(type="verification", options=list(SCHEMA_CHOICES["verification"]), state=BASE)
                 for _ in range(5)]
    await asyncio.gather(*(engine.decide(q, Evidence(items=[result_ev(1), diff_ev(1)])) for q in questions))
    assert engine.loaded and backend.calls == 1  # five concurrent decisions → one forward pass

    def missing() -> Any:
        raise FileNotFoundError("model.safetensors not found")

    broken = LayaEngine(model_id="convaiinnovations/laya", backend_factory=missing)
    assert await broken.available()  # unknown until the first load attempt
    from backend.decision.interface import EngineUnavailable

    with pytest.raises(EngineUnavailable):
        await broken.decide(questions[0], None)
    assert not await broken.available() and "could not load Laya weights" in (broken.describe()["load_error"] or "")
    real = LayaEngine(model_id="convaiinnovations/laya")
    assert await real.available() is False  # the laya package is not installed in this environment


# --------------------------------------------------------------------------- ensemble policy
@pytest.fixture
async def sessions(tmp_path: Path) -> Any:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}"
    await run_migrations(url)
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


def service(sessions: Any, laya_backend: Any = TrainedLayaMock, jev: JevEngine | None = None) -> DecisionService:
    return DecisionService(sessions, laya=LayaEngine(model_id="mock", backend_factory=laya_backend),
                           jev=jev or JevEngine(None, None, httpx.AsyncClient()))


async def logged(sessions: Any) -> list[Decision]:
    async with sessions() as session:
        return list(await session.scalars(select(Decision).order_by(Decision.created_at)))


async def test_laya_shadow_mode_until_validated(sessions: Any) -> None:
    svc = service(sessions)
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(4), diff_ev(2)]))
    assert outcome.result.engine == "rules" and outcome.result.fallback_used
    assert outcome.shadow and outcome.shadow[0].engine == "laya" and outcome.shadow[0].shadow
    rows = await logged(sessions)
    assert [(r.engine, r.acted, r.shadow) for r in rows] == [("laya", False, True), ("rules", True, False)]
    assert rows[0].input_state_json["question"]["state"]["iteration"] == 0  # state seen is logged

    await svc.save_config({"validated": {"verification": True}})
    promoted = await svc.verify_work(BASE, Evidence(items=[result_ev(4), diff_ev(2)]))
    assert promoted.result.engine == "laya" and not promoted.result.fallback_used
    assert await svc.active_engine() == "Laya"


async def test_disagreement_on_confirm_escalates(sessions: Any) -> None:
    class AlwaysConfirm(TrainedLayaMock):
        def predict_batch(self, states: list[str], questions: dict[str, Any]) -> list[dict[str, Any]]:
            options = list(questions["decision"]["criteria"])
            choice = "confirm_done" if "confirm_done" in options else options[0]
            from backend.decision.laya_mock import _answer

            return [_answer(options, choice, 0.97) for _ in states]

    svc = service(sessions, AlwaysConfirm)
    await svc.save_config({"validated": {"verification": True}})
    # Tests FAIL but the model confirms → never confirmed: escalate.
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(1, failed=3), diff_ev(1)]))
    assert outcome.result.choice == "escalate" and "disagree" in outcome.result.reason


async def test_low_confidence_and_high_risk(sessions: Any) -> None:
    svc = service(sessions)
    await svc.save_config({"escalation_threshold": 0.95})
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(3), diff_ev(1)]))
    assert outcome.result.choice == "escalate" and "below escalation threshold" in outcome.result.reason
    await svc.save_config({"escalation_threshold": 0.65, "validated": {"verification": True}})
    risky = await svc.verify_work({**BASE, "high_risk": True}, Evidence(items=[result_ev(3), diff_ev(1)]))
    assert risky.result.engine == "rules" and risky.shadow[0].reason.endswith("high-risk action")


async def test_jev_fallback_then_rules(sessions: Any) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200)
        options = list(json.loads(request.content)["questions"]["decision"]["criteria"])
        choice = "confirm_done" if "confirm_done" in options else options[0]
        return httpx.Response(200, json={"answers": {"decision": {"choice": choice, "confidence": 0.9}}})

    jev = JevEngine("https://jev.test", "k", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    svc = service(sessions, laya_backend=lambda: (_ for _ in ()).throw(FileNotFoundError("no weights")), jev=jev)
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(3), diff_ev(1)]))
    assert outcome.result.engine == "jev" and outcome.result.choice == "confirm_done"
    assert await svc.active_engine() == "Jev"

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    svc.jev = JevEngine("https://jev.test", "k", httpx.AsyncClient(transport=httpx.MockTransport(down)))
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(3), diff_ev(1)]))
    assert outcome.result.engine == "rules"


# --------------------------------------------------------------------------- eval + promotion gate
async def test_eval_gate_promotes_only_good_engines(sessions: Any) -> None:
    items = seed_dataset()
    good = service(sessions, TrainedLayaMock)
    report = await run_eval(good, items, ["rules", "laya"], GateCriteria(min_items=25), promote=True)
    laya = report.engines["laya"]
    assert laya.verification_accuracy == 1.0 and laya.incorrect_completion_detection == 1.0
    assert laya.reliability == 1.0 and laya.latency_p95_ms > 0
    assert set(report.promoted) == {"verification", "routing"} and good.config.validated["verification"]

    bad = service(sessions, ZeroShotLayaMock)
    await bad.save_config({"validated": {"verification": False, "routing": False}})
    report = await run_eval(bad, items, ["laya"], GateCriteria(min_items=25), promote=True)
    assert report.promoted == [] and not bad.config.validated["verification"]
    assert (report.engines["laya"].verification_accuracy or 0) < 0.85


async def test_export_labelled_decisions(sessions: Any, tmp_path: Path) -> None:
    svc = service(sessions)
    outcome = await svc.verify_work(BASE, Evidence(items=[result_ev(1, failed=1), diff_ev(1)]))
    async with sessions() as session:
        row = await session.get(Decision, outcome.decision_id)
        row.label = "restart_same_agent"
        await session.commit()
    path, count = await export_logged(sessions, tmp_path / "out.jsonl")
    assert count == 1
    item = load_jsonl(path)[0]
    assert isinstance(item, DatasetItem) and item.label == "restart_same_agent"
    assert item.evidence_obj().latest("test_result").failed == 1
    _, weak = await export_logged(sessions, tmp_path / "weak.jsonl", include_engine_choices=True)
    assert weak >= 1
