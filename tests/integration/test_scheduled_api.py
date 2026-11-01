"""Scheduled-task API: CRUD, run-now, and enable/disable through the harness."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_scheduled_task_crud_and_run_now(harness) -> None:
    client = harness.client
    created = await client.post("/api/scheduled", json={
        "name": "Hourly check", "prompt": "check the repo", "mode": "chat", "interval_minutes": 60})
    assert created.status_code == 201
    task = created.json()
    assert task["enabled"] is True and task["interval_minutes"] == 60

    listed = (await client.get("/api/scheduled")).json()
    assert [t["id"] for t in listed] == [task["id"]]

    paused = await client.patch(f"/api/scheduled/{task['id']}", json={"enabled": False})
    assert paused.status_code == 200 and paused.json()["enabled"] is False

    run_now = await client.post(f"/api/scheduled/{task['id']}/run")
    assert run_now.status_code == 200 and run_now.json()["run_id"].startswith("run_")

    deleted = await client.delete(f"/api/scheduled/{task['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/scheduled")).json() == []


@pytest.mark.asyncio
async def test_scheduled_task_validation_and_isolation(harness) -> None:
    client = harness.client
    bad = await client.post("/api/scheduled", json={"name": "x", "prompt": "y", "interval_minutes": 0})
    assert bad.status_code == 422
    missing = await client.get("/api/scheduled/missing_id")
    assert missing.status_code == 404
