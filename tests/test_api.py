from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.schemas import Evidence
from tests.conftest import FakeProvider, StubDecisionEngine


async def passing_evidence(*_args, **_kwargs) -> Evidence:
    return Evidence(test_passed=True, tests_run=1, exec_log_tail="1 passed")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    # Run tasks explicitly in the test so WebSocket assertions are timing-independent.
    def suppress_background(coro):
        coro.close()
        return None

    monkeypatch.setattr(main.asyncio, "create_task", suppress_background)
    monkeypatch.setattr(main, "get_decision_engine", lambda: StubDecisionEngine())
    monkeypatch.setattr(main, "complete", FakeProvider().complete)
    monkeypatch.setattr(main, "collect_test_evidence", passing_evidence)
    with TestClient(main.app) as test_client:
        yield test_client


def create_and_complete(client: TestClient, text: str = "Build a feature") -> str:
    response = client.post("/api/runs", json={"text": text, "mode": "build"})
    assert response.status_code == 202
    run_id = response.json()["id"]
    asyncio.run(main.run_agent(main.RUNS[run_id]))
    return run_id


def test_task_streams_agent_subagent_decision_and_done_events(client: TestClient):
    run_id = create_and_complete(client)

    with client.websocket_connect(f"/api/runs/{run_id}/stream") as socket:
        events = [socket.receive_json() for _ in range(len(main.EVENTS[run_id]) + 1)]

    types = {event["type"] for event in events}
    assert {"agent_started", "subagent_called", "decision", "run_finished"} <= types
    assert all(event["run_id"] == run_id for event in events)


def test_api_rejects_malformed_run_and_unknown_websocket(client: TestClient):
    assert client.post("/api/runs", json={"mode": "build"}).status_code == 422
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/not-found/stream"):
            pass


def test_provider_and_router_configuration_apis_redact_keys(client: TestClient):
    created = client.post("/api/providers", json={
        "name": "Custom local", "base_url": "http://localhost:11434/v1", "api_key": "test-token", "kind": "custom",
    })
    assert created.status_code == 201
    provider = created.json()
    assert provider["api_key"] == "" and provider["has_api_key"] is True
    assert client.get("/api/providers").json()[0]["api_key"] == ""

    config = {"tiers": {"code_generation": {"provider": provider["id"], "model": "local-coder"}}}
    assert client.put("/api/router-config", json=config).json() == config
    assert client.get("/api/router-config").json() == config
    assert client.delete(f"/api/providers/{provider['id']}").status_code == 204


def test_provider_models_endpoint_returns_router_models_without_key(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    created = client.post("/api/providers", json={
        "name": "Custom local", "base_url": "http://localhost:11434/v1", "api_key": "test-token", "kind": "custom",
    }).json()
    seen = {}

    async def models(provider):
        seen["provider"] = provider
        return ["coder-small", "coder-large"]

    monkeypatch.setattr(main, "list_models", models)
    response = client.get(f"/api/providers/{created['id']}/models")

    assert response.json() == {"provider_id": created["id"], "models": ["coder-small", "coder-large"]}
    assert seen["provider"]["api_key"] == "test-token"


def test_mid_run_disconnect_cleans_up_subscriber(client: TestClient):
    run_id = create_and_complete(client)
    with client.websocket_connect(f"/api/runs/{run_id}/stream") as socket:
        socket.receive_json()

    assert not main.SUBSCRIBERS.get(run_id, set())


def test_concurrent_sessions_do_not_leak_run_events(client: TestClient):
    first = create_and_complete(client, "Build first")
    second = create_and_complete(client, "Build second")

    with client.websocket_connect(f"/api/runs/{first}/stream") as first_socket, client.websocket_connect(f"/api/runs/{second}/stream") as second_socket:
        first_events = [first_socket.receive_json() for _ in range(len(main.EVENTS[first]) + 1)]
        second_events = [second_socket.receive_json() for _ in range(len(main.EVENTS[second]) + 1)]

    assert {event["run_id"] for event in first_events} == {first}
    assert {event["run_id"] for event in second_events} == {second}
