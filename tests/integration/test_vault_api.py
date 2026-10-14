from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete

from backend.db.models import Run
from tests.conftest import AppHarness
from tests.integration.test_runs_api import wait_terminal
from tests.support.fake_runner import FakeRunner, install


async def test_run_written_to_vault_and_tree_matches(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    client = harness.client
    run_id = (await client.post("/api/runs", json={"text": "vault me", "mode": "build"})).json()["id"]
    await wait_terminal(client, run_id)
    for _ in range(50):
        run = (await client.get(f"/api/runs/{run_id}")).json()
        if run["vault_note_path"]:
            break
        import asyncio

        await asyncio.sleep(0.05)
    assert run["vault_note_path"] and run["vault_note_path"].startswith("Jevora/Runs/")
    from_vault = (await client.get(f"/api/vault/runs/{run_id}")).json()
    api_tree = (await client.get(f"/api/runs/{run_id}/tree")).json()
    assert from_vault["tree"] == api_tree
    listing = (await client.get("/api/vault/runs")).json()["runs"]
    assert listing[0]["run_id"] == run_id and listing[0]["verdict"] == "confirm_done"


async def test_reindex_rebuilds_history_from_vault(harness: AppHarness) -> None:
    install(harness.ctx, FakeRunner())
    client = harness.client
    run_id = (await client.post("/api/runs", json={"text": "portable", "mode": "build"})).json()["id"]
    await wait_terminal(client, run_id)
    import asyncio

    for _ in range(50):
        if (await client.get(f"/api/runs/{run_id}")).json()["vault_note_path"]:
            break
        await asyncio.sleep(0.05)
    before = (await client.get(f"/api/runs/{run_id}/tree")).json()
    async with harness.ctx.sessions() as session:
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.commit()
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 404
    result = (await client.post("/api/vault/reindex")).json()
    assert result["imported"] == 1
    after = (await client.get(f"/api/runs/{run_id}/tree")).json()
    assert after == before
    assert "+print('hi')" in (await client.get(f"/api/runs/{run_id}/diff")).json()["diff"]
    assert (await client.post("/api/vault/reindex")).json() == {"imported": 0, "skipped": 1, "total": 1}


async def test_vault_config_and_unwritable_path(harness: AppHarness, tmp_path: Path) -> None:
    client = harness.client
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    response = (await client.put("/api/vault/config", json={"path": str(blocker)})).json()
    assert response["writable"] is False and response["error"]
    health = (await client.get("/api/system/health", params={"probe": "false"})).json()
    assert health["vault"]["writable"] is False

    # A run still completes when the vault cannot be written; an error event is emitted.
    install(harness.ctx, FakeRunner())
    run_id = (await client.post("/api/runs", json={"text": "no vault", "mode": "chat"})).json()["id"]
    run = await wait_terminal(client, run_id)
    assert run["status"] == "done"
    errors = [e for e in harness.ctx.events.history(run_id) if e.type == "error"]
    import asyncio

    for _ in range(40):
        if errors:
            break
        await asyncio.sleep(0.05)
        errors = [e for e in harness.ctx.events.history(run_id) if e.type == "error"]
    assert errors and errors[0].data["source"] == "vault"

    good = tmp_path / "fresh" / "vault"
    assert (await client.put("/api/vault/config", json={"path": str(good)})).json()["writable"] is True
