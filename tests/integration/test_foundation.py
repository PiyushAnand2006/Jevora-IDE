from __future__ import annotations

from pathlib import Path

import httpx
from sqlalchemy import create_engine, inspect, text

from backend.db.session import downgrade_sync, upgrade_sync


async def test_static_frontend_served(client: httpx.AsyncClient) -> None:
    index = await client.get("/")
    assert index.status_code == 200 and "Jevora IDE" in index.text
    assert (await client.get("/app.js")).status_code == 200
    assert (await client.get("/auth.html")).status_code == 200


async def test_health_and_features(client: httpx.AsyncClient) -> None:
    health = await client.get("/api/system/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok" and body["database"]["ok"] is True
    features = (await client.get("/api/features")).json()
    assert features["web_search"] is False


async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    await client.get("/api/providers")
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200 and "jevora_http_requests_total" in metrics.text


def test_migration_upgrade_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    upgrade_sync(url, "0001")
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email, name, is_local, created_at) "
                          "VALUES ('u1', 'a@b.c', 'A', 0, '2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO runs (id, user_id, mode, task, attachments, status, created_at, "
                          "final_output, iteration_count, total_tokens, cost_estimate, cancelled, meta) "
                          "VALUES ('r1', 'u1', 'build', 't', '[]', 'done', '2026-01-01 00:00:00', "
                          "'', 0, 0, 0, 0, '{}')"))
        conn.execute(text("INSERT INTO decisions (id, run_id, type, engine, input_state_json, evidence_ref, "
                          "choice, confidence, alternatives, latency_ms, fallback_used, shadow, acted, reason, "
                          "created_at) VALUES ('d1', 'r1', 'verification', 'rules', '{}', '[]', 'confirm_done', "
                          "0.9, '{}', 1, 0, 0, 1, '', '2026-01-01 00:00:00')"))
    cols = {c["name"] for c in inspect(engine).get_columns("decisions")}
    assert "label" not in cols
    upgrade_sync(url, "head")
    inspector = inspect(engine)
    assert {"label", "label_note", "labelled_at"} <= {c["name"] for c in inspector.get_columns("decisions")}
    assert "llm_calls" in inspector.get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT choice FROM decisions WHERE id='d1'")).scalar() == "confirm_done"
    downgrade_sync(url, "0001")
    assert "llm_calls" not in inspect(engine).get_table_names()
    engine.dispose()
