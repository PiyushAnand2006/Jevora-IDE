from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from tests.conftest import AppHarness, add_mock_provider, make_settings
from tests.support.servers import MockLLM


# --------------------------------------------------------------------------- workspace API
async def test_workspace_tree_file_write_and_changes(harness: AppHarness) -> None:
    client = harness.client
    ws = harness.ctx.settings.workspace_root
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_bytes(b"print('x')\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (ws / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    tree = (await client.get("/api/workspace/tree")).json()
    names = {c["name"] for c in tree["children"]}
    assert {"src", "logo.png"} <= names and "node_modules" not in names
    src = next(c for c in tree["children"] if c["name"] == "src")
    assert src["children"][0]["language"] == "python"

    file = (await client.get("/api/workspace/file", params={"path": "src/main.py"})).json()
    assert file["content"] == "print('x')\n" and file["language"] == "python" and file["binary"] is False
    binary = (await client.get("/api/workspace/file", params={"path": "logo.png"})).json()
    assert binary["binary"] is True and binary["content"] is None
    raw = await client.get("/api/workspace/file", params={"path": "logo.png", "raw": "true"})
    assert raw.content.startswith(b"\x89PNG")

    written = await client.put("/api/workspace/file", json={"path": "src/new 文件.md", "content": "# ünïcode\n"})
    assert written.status_code == 200 and written.json()["created"] is True
    assert (ws / "src" / "new 文件.md").read_text(encoding="utf-8") == "# ünïcode\n"
    feed = (await client.get("/api/workspace/changes")).json()
    assert feed["items"][-1]["path"] == "src/new 文件.md" and feed["items"][-1]["change"] == "created"
    later = (await client.get("/api/workspace/changes", params={"since_seq": feed["last_seq"]})).json()
    assert later["items"] == []

    assert (await client.get("/api/workspace/file", params={"path": "../secret"})).status_code == 403
    assert (await client.put("/api/workspace/file", json={"path": "../../x", "content": ""})).status_code == 403
    assert (await client.put("/api/workspace/file", json={"path": ".git/config", "content": ""})).status_code == 403
    assert (await client.get("/api/workspace/file", params={"path": "missing.py"})).status_code == 404
    assert (await client.delete("/api/workspace/file", params={"path": "src/new 文件.md"})).status_code == 200


# --------------------------------------------------------------------------- terminal
def test_terminal_pty_roundtrip(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client, client.websocket_connect("/api/terminal?cols=100&rows=30") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["sandbox"]["kind"] == "local"
        ws.send_json({"type": "resize", "cols": 120, "rows": 40})
        ws.send_json({"type": "stdin", "data": "echo jevora-pty-ok\r"})
        seen = ""
        deadline = time.time() + 20
        while "jevora-pty-ok" not in seen.replace("echo jevora-pty-ok", "") and time.time() < deadline:
            message = ws.receive_json()
            if message["type"] == "stdout":
                seen += message["data"]
        assert "jevora-pty-ok" in seen.replace("echo jevora-pty-ok", "")


def test_terminal_reports_unavailable_sandbox(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, sandbox_mode="local", local_unsafe_exec=False))
    with TestClient(app) as client, client.websocket_connect("/api/terminal") as ws:
        message = ws.receive_json()
        assert message["type"] == "error" and "LOCAL_UNSAFE_EXEC" in message["message"]


# --------------------------------------------------------------------------- agents API
async def test_agent_crud_and_status(harness: AppHarness, mock_llm: MockLLM) -> None:
    client = harness.client
    provider = await add_mock_provider(client, mock_llm)
    created = await client.post("/api/agents", json={
        "name": "api-tester", "template": "tester", "description": "tests APIs",
        "model_binding": {"mode": "explicit", "provider_id": provider["id"], "model": "mock-mini"}})
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["status"] == "idle" and agent["config"]["tool_permissions"]
    assert (await client.post("/api/agents", json={"name": "api-tester", "template": "coder"})).status_code == 409
    bad = await client.post("/api/agents", json={"name": "x", "tool_permissions": ["rm.rf"]})
    assert bad.status_code == 422
    bad_provider = await client.post("/api/agents", json={
        "name": "y", "model_binding": {"mode": "explicit", "provider_id": "prv_nope", "model": "m"}})
    assert bad_provider.status_code == 422
    updated = await client.put(f"/api/agents/{agent['id']}", json={"description": "updated"})
    assert updated.json()["description"] == "updated"
    listing = (await client.get("/api/agents")).json()
    assert [a["name"] for a in listing] == ["api-tester"]

    status = (await client.get("/api/agents/status")).json()
    assert set(status) >= {"agents_running", "subagents_running", "vms"}
    templates = (await client.get("/api/agents/templates")).json()
    assert {t["id"] for t in templates} >= {"planner", "coder", "screen-tester"}

    # Deleting the provider unbinds the agent (falls back to Auto).
    await client.delete(f"/api/providers/{provider['id']}")
    after = (await client.get(f"/api/agents/{agent['id']}")).json()
    assert after["config"]["model_binding"]["mode"] == "auto"
    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 200
    assert (await client.get(f"/api/agents/{agent['id']}")).status_code == 404


async def test_generate_agent_with_llm_and_fallback(harness: AppHarness, mock_llm: MockLLM) -> None:
    client = harness.client
    no_llm = await client.post("/api/agents/generate", json={"description": "An agent that writes pytest tests"})
    assert no_llm.status_code == 201
    body = no_llm.json()
    assert body["generated_by"] == "heuristic" and body["template"] == "tester" and body["saved"] is True

    await add_mock_provider(client, mock_llm)
    mock_llm.replies('{"name": "Docs Writer", "description": "Writes docs", "template": "coder", '
                     '"system_prompt": "You write documentation. Be precise. Cite files.", '
                     '"tool_permissions": ["fs.read", "fs.write", "fs.search"], "category": "creative_ui", '
                     '"memory_scope": "workspace", "max_depth": 1}')
    generated = (await client.post("/api/agents/generate", json={"description": "documentation writer"})).json()
    assert generated["generated_by"] == "llm" and generated["name"] == "docs-writer"
    assert generated["config"]["tool_permissions"] == ["fs.read", "fs.write", "fs.search"]
    assert generated["config"]["model_binding"] == {"mode": "auto", "category": "creative_ui",
                                                    "provider_id": None, "model": None}
    again = (await client.post("/api/agents/generate", json={"description": "documentation writer"})).json()
    assert again["name"] == "docs-writer-2"


async def test_vms_api(harness: AppHarness) -> None:
    client = harness.client
    listing = (await client.get("/api/vms")).json()
    assert listing["vms"][0]["id"] == "default" and listing["vms"][0]["selected"] is True
    vm = (await client.post("/api/vms", json={"name": "scratch"})).json()
    assert vm["kind"] == "local" and vm["status"] == "stopped"
    started = (await client.post(f"/api/vms/{vm['id']}/start")).json()
    assert started["status"] == "running"
    selected = (await client.post(f"/api/vms/{vm['id']}/select")).json()
    assert next(v for v in selected["vms"] if v["id"] == vm["id"])["selected"] is True
    stopped = (await client.post(f"/api/vms/{vm['id']}/stop")).json()
    assert stopped["status"] == "stopped"
    assert (await client.post("/api/vms/default/select")).status_code == 200
    assert (await client.delete(f"/api/vms/{vm['id']}")).status_code == 200
    assert (await client.delete("/api/vms/default")).status_code == 422


def _unused(_: httpx.Response) -> None:
    pass
