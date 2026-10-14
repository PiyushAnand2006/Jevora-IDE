from __future__ import annotations

from tests.conftest import AppHarness, add_mock_provider
from tests.integration.test_runs_api import wait_terminal
from tests.support.scenarios import build_brain
from tests.support.servers import MockLLM


async def test_decision_config_api(harness: AppHarness) -> None:
    client = harness.client
    current = (await client.get("/api/decision/config")).json()
    assert current["config"]["escalation_threshold"] == 0.65
    assert current["status"]["active"] == "Rules"  # Laya disabled in tests, Jev not configured
    assert current["status"]["primary"]["mode"] == "shadow"
    updated = (await client.put("/api/decision/config", json={"escalation_threshold": 0.7,
                                                              "thresholds": {"laya": {"verification": 0.9}}})).json()
    assert updated["config"]["escalation_threshold"] == 0.7
    assert updated["config"]["thresholds"]["laya"]["verification"] == 0.9
    assert updated["config"]["thresholds"]["laya"]["routing"] == 0.8  # untouched keys kept
    assert (await client.put("/api/decision/config", json={"bogus": 1})).status_code == 422
    assert (await client.put("/api/decision/config", json={"escalation_threshold": 3})).status_code == 422
    health = (await client.get("/api/system/health", params={"probe": "false"})).json()
    assert health["decision_engine"]["active"] == "Rules" and health["orchestrator"]["engine"] == "LangGraph"


async def test_label_decisions_and_eval_api(harness: AppHarness, mock_llm: MockLLM) -> None:
    client = harness.client
    await add_mock_provider(client, mock_llm)
    brain, _ = build_brain()
    brain.install(mock_llm)
    run_id = (await client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    run = await wait_terminal(client, run_id, timeout=60)
    decision = run["decisions"][-1]
    assert (await client.post(f"/api/decisions/{decision['id']}/label", json={"label": "nonsense"})).status_code == 422
    labelled = await client.post(f"/api/decisions/{decision['id']}/label",
                                 json={"label": "confirm_done", "note": "reviewed by me"})
    assert labelled.status_code == 200 and labelled.json()["label"] == "confirm_done"
    listing = (await client.get("/api/decisions", params={"run_id": run_id, "labelled": "true"})).json()
    assert [d["id"] for d in listing["items"]] == [decision["id"]]
    assert (await client.get("/api/decision/eval/latest")).status_code == 404
    report = (await client.post("/api/decision/eval", json={"engines": ["rules", "laya"]})).json()
    assert report["report"]["engines"]["rules"]["verification_accuracy"] == 1.0
    assert report["report"]["engines"]["laya"]["available"] is False
    assert report["report"]["dataset_origin"]["logged"] == 1
    assert (await client.get("/api/decision/eval/latest")).status_code == 200
