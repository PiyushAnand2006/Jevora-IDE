"""End-to-end checks for the new usage/tools/projects/scheduled endpoints."""
import pytest


@pytest.mark.asyncio
async def test_usage_tools_projects_scheduled_live(harness) -> None:
    client = harness.client
    usage = (await client.get("/api/usage?days=7&group_by=day")).json()
    assert usage["totals"]["calls"] >= 0 and "groups" in usage

    tools = (await client.get("/api/tools")).json()
    assert len(tools) > 5
    assert all("name" in t and "permission" in t for t in tools)
    perms = {t["permission"] for t in tools}
    assert any(p.startswith("fs.") for p in perms)

    projects = (await client.get("/api/projects")).json()
    assert isinstance(projects, list)
    created = (await client.post("/api/projects", json={"name": "Test Project"})).json()
    assert created["id"].startswith("ws_")
    dup = await client.post("/api/projects", json={"name": "Test Project"})
    assert dup.status_code == 409
    assert any(p["id"] == created["id"] for p in (await client.get("/api/projects")).json())

    task = (await client.post("/api/scheduled", json={
        "name": "Daily check", "prompt": "check", "mode": "chat", "interval_minutes": 1440})).json()
    assert task["enabled"] is True
    assert (await client.delete(f"/api/scheduled/{task['id']}")).status_code == 200
