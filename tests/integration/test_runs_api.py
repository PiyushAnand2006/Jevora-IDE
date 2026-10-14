from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from tests.conftest import AppHarness, make_settings
from tests.support.fake_runner import FakeRunner, install

TERMINAL = {"done", "failed", "escalated"}


async def wait_terminal(client: httpx.AsyncClient, run_id: str, timeout: float = 20) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        run = (await client.get(f"/api/runs/{run_id}")).json()
        if run["status"] in TERMINAL:
            return run
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} stuck in {run['status']}")
        await asyncio.sleep(0.05)


async def test_run_lifecycle_matches_frontend_contract(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    created = await harness.client.post("/api/runs", json={"text": "build a thing", "mode": "build"})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "queued" and body["id"].startswith("run_")
    run = await wait_terminal(harness.client, body["id"])
    assert run["status"] == "done"
    assert isinstance(run["final_output"], str) and "Hello world" in run["final_output"]
    last = run["decisions"][-1]
    assert last["choice"] == "confirm_done" and 0 <= last["confidence"] <= 1
    assert run["agents"] == {"total": 1, "subagents": 1, "running": 0}
    assert run["models_used"] == ["mock-mini", "mock-pro"]
    assert run["total_tokens"] == 42


async def test_run_validation_edge_cases(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    client = harness.client
    assert (await client.post("/api/runs", json={"text": "   ", "mode": "build"})).status_code == 422
    assert (await client.post("/api/runs", json={"text": "x", "mode": "cooking"})).status_code == 422
    huge = await client.post("/api/runs", json={"text": "a" * 100_001, "mode": "chat"})
    assert huge.status_code == 413 and huge.json()["code"] == "prompt_too_long"
    general = await client.post("/api/runs", json={"text": "research it", "mode": "general"})
    assert general.status_code == 201 and general.json()["mode"] == "research"
    text = "Créer une API 🚀 — 日本語のテスト"
    unicode_run = await client.post("/api/runs", json={"text": text, "mode": "chat"})
    assert unicode_run.status_code == 201
    run = await wait_terminal(client, unicode_run.json()["id"])
    assert run["task"] == text
    assert (await client.get("/api/runs/run_doesnotexist")).status_code == 404


async def test_failed_run_reports_error(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner(fail=True))
    created = (await harness.client.post("/api/runs", json={"text": "x", "mode": "build"})).json()
    run = await wait_terminal(harness.client, created["id"])
    assert run["status"] == "failed" and "scripted failure" in run["error"]


async def test_history_pagination_filters_and_delete(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    ids = []
    for i, mode in enumerate(["build", "chat", "build", "analysis", "build"]):
        ids.append((await harness.client.post("/api/runs", json={"text": f"task {i} alpha" if i % 2 else f"task {i}",
                                                                   "mode": mode})).json()["id"])
    for run_id in ids:
        await wait_terminal(harness.client, run_id)
    page1 = (await harness.client.get("/api/runs", params={"limit": 2})).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = (await harness.client.get("/api/runs", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    page3 = (await harness.client.get("/api/runs", params={"limit": 2, "cursor": page2["next_cursor"]})).json()
    seen = [r["id"] for r in page1["items"] + page2["items"] + page3["items"]]
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5 and page3["next_cursor"] is None
    builds = (await harness.client.get("/api/runs", params={"mode": "build"})).json()["items"]
    assert len(builds) == 3
    alpha = (await harness.client.get("/api/runs", params={"q": "alpha"})).json()["items"]
    assert len(alpha) == 2
    assert all(item["verdict"]["choice"] == "confirm_done" for item in builds)
    done = (await harness.client.get("/api/runs", params={"status": "done"})).json()["items"]
    assert len(done) == 5
    assert (await harness.client.delete(f"/api/runs/{ids[0]}")).status_code == 200
    assert (await harness.client.get(f"/api/runs/{ids[0]}")).status_code == 404


async def test_tree_endpoint(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    run_id = (await harness.client.post("/api/runs", json={"text": "tree", "mode": "build"})).json()["id"]
    await wait_terminal(harness.client, run_id)
    tree = (await harness.client.get(f"/api/runs/{run_id}/tree")).json()
    assert tree["agents"] == 1 and tree["subagents"] == 1
    root = tree["roots"][0]
    assert root["role"] == "planner" and root["model_used"] == "mock-pro" and root["status"] == "done"
    child = root["children"][0]
    assert child["role"] == "coder" and child["parent_call_id"] == root["call_id"] and child["depth"] == 1
    assert child["decisions"][0]["choice"] == "confirm_done"
    assert child["duration_ms"] is not None
    diff = (await harness.client.get(f"/api/runs/{run_id}/diff")).json()
    assert "+print('hi')" in diff["diff"] and diff["stats"]["files_changed"] == 1


async def test_concurrent_runs_are_bounded_and_fair(tmp_path: Path) -> None:
    from tests.conftest import start_app

    settings = make_settings(tmp_path, max_concurrent_runs=2)
    app, lifespan = await start_app(settings)
    ctx = app.state.ctx
    runner = install(ctx, FakeRunner(delay=0.15))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        ids = [(await client.post("/api/runs", json={"text": f"r{i}", "mode": "chat"})).json()["id"]
               for i in range(6)]
        await asyncio.gather(*(wait_terminal(client, r) for r in ids))
    assert runner.max_concurrent == 2
    await lifespan.__aexit__(None, None, None)


async def test_fair_share_between_users(tmp_path: Path) -> None:
    from tests.conftest import start_app

    settings = make_settings(tmp_path, max_concurrent_runs=1)
    app, lifespan = await start_app(settings)
    ctx = app.state.ctx
    gate = asyncio.Event()
    runner = install(ctx, FakeRunner(block=gate))
    from backend.db.models import User

    async with ctx.sessions() as session:
        session.add_all([User(id="usr_a", email="a@x.io", name="A"), User(id="usr_b", email="b@x.io", name="B")])
        await session.commit()
    for _ in range(3):
        await ctx.runs.create("usr_a", text="a", mode="chat")
    for _ in range(2):
        await ctx.runs.create("usr_b", text="b", mode="chat")
    await asyncio.sleep(0.2)
    gate.set()
    for _ in range(100):
        if len(runner.order) == 5 and ctx.executor.active_count == 0:
            break
        await asyncio.sleep(0.05)
    # The first slot goes to A; after that the users alternate instead of A draining its queue.
    try:
        assert runner.order == ["usr_a", "usr_b", "usr_a", "usr_b", "usr_a"]
    finally:
        await lifespan.__aexit__(None, None, None)


async def test_cancel_running_run(harness: AppHarness) -> None:
    gate = asyncio.Event()
    install(harness.ctx, FakeRunner(block=gate))
    run_id = (await harness.client.post("/api/runs", json={"text": "slow", "mode": "build"})).json()["id"]
    for _ in range(100):
        if (await harness.client.get(f"/api/runs/{run_id}")).json()["status"] == "planning":
            break
        await asyncio.sleep(0.02)
    cancelled = (await harness.client.post(f"/api/runs/{run_id}/cancel")).json()
    assert cancelled["status"] == "failed" and cancelled["cancelled"] is True
    tree = (await harness.client.get(f"/api/runs/{run_id}/tree")).json()
    statuses = {tree["roots"][0]["status"], tree["roots"][0]["children"][0]["status"]}
    assert statuses == {"cancelled"}
    resume = await harness.client.post(f"/api/runs/{run_id}/resume")
    assert resume.status_code == 409


async def test_sse_events_replay(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    run_id = (await harness.client.post("/api/runs", json={"text": "sse", "mode": "build"})).json()["id"]
    response = await harness.client.get(f"/api/runs/{run_id}/events", timeout=20)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert "agent.started" in types and "subagent.spawned" in types and "decision.made" in types
    assert types[-1] == "run.status" and events[-1]["data"]["status"] == "done"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    mid = seqs[len(seqs) // 2]
    replay = await harness.client.get(f"/api/runs/{run_id}/events", params={"last_seq": mid})
    replayed = [json.loads(line[6:])["seq"] for line in replay.text.splitlines() if line.startswith("data: ")]
    assert replayed and min(replayed) > mid


def test_websocket_stream_and_reconnect_replay(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        gate_holder: dict[str, Any] = {}

        async def setup() -> None:
            gate_holder["gate"] = asyncio.Event()
            install(app.state.ctx, FakeRunner(block=gate_holder["gate"]))

        client.portal.call(setup)  # type: ignore[union-attr]
        run_id = client.post("/api/runs", json={"text": "ws", "mode": "build"}).json()["id"]
        received: list[dict[str, Any]] = []
        with client.websocket_connect(f"/api/runs/{run_id}/stream") as ws:
            while True:
                msg = ws.receive_json()
                received.append(msg)
                if msg["type"] == "token.delta" and msg["data"]["text"] == "world":
                    break
        last_seq = received[-1]["seq"]
        client.portal.call(gate_holder["gate"].set)  # type: ignore[union-attr]
        after: list[dict[str, Any]] = []
        with client.websocket_connect(f"/api/runs/{run_id}/stream?last_seq={last_seq}") as ws:
            while True:
                msg = ws.receive_json()
                after.append(msg)
                if msg["type"] == "run.status" and msg["data"]["status"] == "done":
                    break
        assert all(m["seq"] > last_seq for m in after)
        assert {"agent.finished", "decision.made"} <= {m["type"] for m in after}
        assert "token.delta" not in {m["type"] for m in after}  # nothing replayed twice

        with client.websocket_connect("/api/events") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            client.post("/api/runs", json={"text": "global", "mode": "chat"})
            while True:
                msg = ws.receive_json()
                if msg["type"] == "run.created":
                    break


def test_websocket_rejects_unknown_run(tmp_path: Path) -> None:
    from starlette.websockets import WebSocketDisconnect

    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/runs/run_nope/stream") as ws:
                ws.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4404
        else:
            raise AssertionError("expected the socket to be closed")
