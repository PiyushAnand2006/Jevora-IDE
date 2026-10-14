from __future__ import annotations

import io
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.conftest import AppHarness, add_mock_provider, make_settings, start_app
from tests.integration.test_runs_api import wait_terminal
from tests.support.brain import Brain, Turn, call, tools_reply
from tests.support.runctx import make_run_context
from tests.support.servers import MockLLM, MockReply

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------------------------------------------------------- MCP
async def test_mcp_stdio_round_trip(harness: AppHarness) -> None:
    client = harness.client
    created = await client.post("/api/mcp/servers", json={
        "name": "echo", "transport": "stdio", "command": sys.executable,
        "args": [str(FIXTURES / "mcp_echo_server.py")], "env": {"SECRET_TOKEN": "abc"}})
    assert created.status_code == 201, created.text
    server = created.json()
    assert server["status"] == "connected", server
    assert sorted(server["tools"]) == ["add", "echo"] and server["has_env"] is True
    assert "abc" not in created.text
    tools = (await client.get(f"/api/mcp/servers/{server['id']}/tools")).json()["tools"]
    assert {t["name"] for t in tools} == {"add", "echo"}
    result = (await client.post(f"/api/mcp/servers/{server['id']}/tools/echo/call",
                                json={"arguments": {"text": "hi mcp"}})).json()
    assert result == {"ok": True, "content": "HI MCP", "structured": result["structured"]}
    added = (await client.post(f"/api/mcp/servers/{server['id']}/tools/add/call",
                               json={"arguments": {"a": 2, "b": 40}})).json()
    assert "42" in added["content"]
    health = (await client.get(f"/api/mcp/servers/{server['id']}/health")).json()
    assert health["ok"] is True and health["tools"] == 2
    assert (await client.post(f"/api/mcp/servers/{server['id']}/tools/nope/call", json={})).status_code == 404

    # Agents with mcp.call see the server's tools.
    from backend.agents.pool import CallNode
    from backend.agents.templates import template
    from backend.agents.tools.base import ToolContext

    rc = await make_run_context(harness.ctx)
    coder = template("coder")
    node = CallNode(call_id="c", config=coder, key="k", depth=0)
    ctx = ToolContext(app=harness.ctx, run_id=rc.run_id, user_id=rc.user_id, call_id="c", agent=coder, node=node,
                      scope=rc.scope, recorder=rc.recorder, workspace=rc.workspace, guard=rc.guard,
                      sandbox_factory=rc.sandbox)
    names = {t.name for t in await harness.ctx.tools.available(ctx)}
    assert "mcp__echo__echo" in names
    via_agent = await harness.ctx.tools.invoke(ctx, "mcp__echo__echo", {"text": "agent"})
    assert via_agent.ok and via_agent.content == "AGENT"
    critic_ctx = ToolContext(**{**ctx.__dict__, "agent": template("critic")})
    assert "mcp__echo__echo" not in {t.name for t in await harness.ctx.tools.available(critic_ctx)}

    disabled = (await client.patch(f"/api/mcp/servers/{server['id']}", json={"enabled": False})).json()
    assert disabled["enabled"] is False and disabled["status"] in ("disabled", "disconnected")
    listing = (await client.get("/api/mcp/servers")).json()
    assert listing[0]["enabled"] is False
    health_all = (await client.get("/api/system/health", params={"probe": "false"})).json()
    assert [b["name"] for b in health_all["mcp"]["builtin"]] == ["Decision Engine", "LangGraph Orchestrator",
                                                                  "System Tool & Sandbox"]
    assert (await client.delete(f"/api/mcp/servers/{server['id']}")).status_code == 200


async def test_mcp_validation_and_failed_server(harness: AppHarness) -> None:
    client = harness.client
    assert (await client.post("/api/mcp/servers", json={"name": "x", "transport": "ftp"})).status_code == 422
    assert (await client.post("/api/mcp/servers", json={"name": "x", "transport": "stdio"})).status_code == 422
    assert (await client.post("/api/mcp/servers", json={"name": "x", "transport": "http", "url": "nope"})
            ).status_code == 422
    broken = (await client.post("/api/mcp/servers", json={"name": "broken", "transport": "stdio",
                                                          "command": "definitely-not-a-command-xyz"})).json()
    assert broken["status"] == "error" and broken["error"]
    dup = await client.post("/api/mcp/servers", json={"name": "broken", "transport": "stdio", "command": "x"})
    assert dup.status_code == 409


# --------------------------------------------------------------------------- attachments
async def test_attachments_upload_extract_and_limits(harness: AppHarness) -> None:
    client = harness.client
    files = [("files", ("notes.md", b"# Notes\nThe answer is 42.\n", "text/markdown")),
             ("files", ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")),
             ("files", ("blob.bin", bytes(range(256)) * 4, "application/octet-stream")),
             ("files", ("unicode.txt", "héllo — 世界".encode(), "text/plain"))]
    uploaded = (await client.post("/api/attachments", files=files)).json()["attachments"]
    kinds = {a["filename"]: a["kind"] for a in uploaded}
    assert kinds == {"notes.md": "text", "pixel.png": "image", "blob.bin": "binary", "unicode.txt": "text"}
    note = next(a for a in uploaded if a["filename"] == "notes.md")
    assert "The answer is 42." in note["text_preview"]
    detail = (await client.get(f"/api/attachments/{note['id']}")).json()
    assert detail["text"].startswith("# Notes")
    raw = await client.get(f"/api/attachments/{note['id']}/content")
    assert raw.content == b"# Notes\nThe answer is 42.\n"
    stored = harness.ctx.settings.workspace_root / ".jevora" / "uploads"
    assert any(p.name.endswith("notes.md") for p in stored.iterdir())

    exe = await client.post("/api/attachments", files=[("files", ("tool.exe", b"MZ\x90\x00", "application/x-msdownload"))])
    assert exe.status_code == 415
    disguised = await client.post("/api/attachments", files=[("files", ("innocent.txt", b"MZ\x90\x00binary", "text/plain"))])
    assert disguised.status_code == 415
    harness.ctx.settings.max_upload_mb = 1
    huge = await client.post("/api/attachments", files=[("files", ("big.txt", b"a" * (1024 * 1024 + 10), "text/plain"))])
    assert huge.status_code == 413
    assert not any(p.name.endswith("big.txt") for p in stored.iterdir())
    assert (await client.delete(f"/api/attachments/{note['id']}")).status_code == 200


async def test_pdf_attachment_text_extraction(harness: AppHarness) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, StreamObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    stream = StreamObject()
    stream._data = b"BT /F1 12 Tf 20 100 Td (Jevora PDF text) Tj ET"
    font = writer._add_object(__import__("pypdf").generic.DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    page[NameObject("/Resources")] = __import__("pypdf").generic.DictionaryObject({
        NameObject("/Font"): __import__("pypdf").generic.DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    uploaded = (await harness.client.post("/api/attachments", files=[
        ("files", ("doc.pdf", buffer.getvalue(), "application/pdf"))])).json()["attachments"][0]
    assert uploaded["kind"] == "pdf" and "Jevora PDF text" in uploaded["text_preview"]


async def test_attachments_reach_the_agent(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    att = (await harness.client.post("/api/attachments", files=[
        ("files", ("spec.txt", b"Requirement: the API must return HTTP 418.", "text/plain"))])).json()["attachments"][0]

    def chat(turn: Turn) -> MockReply | None:
        if turn.role == "chat":
            assert "HTTP 418" in turn.task
            return MockReply(content="The spec requires HTTP 418.")
        return None

    Brain().on(chat).install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "What does the spec require?", "mode": "chat",
                                                           "attachments": [att["id"]]})).json()["id"]
    run = await wait_terminal(harness.client, run_id)
    assert run["status"] == "done" and "418" in run["final_output"] and run["attachments"] == [att["id"]]


# --------------------------------------------------------------------------- plugins (third-party)
@pytest.fixture
async def plugin_app(tmp_path: Path) -> AsyncIterator[AppHarness]:
    settings = make_settings(tmp_path, plugins_dir=FIXTURES / "plugins")
    app, lifespan = await start_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield AppHarness(app, client)
    await lifespan.__aexit__(None, None, None)


async def test_third_party_plugin_tools_and_hooks(plugin_app: AppHarness, mock_llm: MockLLM) -> None:
    client = plugin_app.client
    plugins = {p["id"]: p for p in (await client.get("/api/plugins")).json()}
    hello = plugins["hello_plugin"]
    assert hello["builtin"] is False and hello["enabled"] is True and hello["tools"] == ["hello"]
    assert set(hello["hooks"]) == {"before_run", "after_node", "on_decision"} and hello["error"] is None
    await add_mock_provider(client, mock_llm)

    def chat(turn: Turn) -> MockReply | None:
        if turn.role == "chat" and turn.step == 0:
            return tools_reply(call("hello", name="Ada"))
        if turn.role == "chat":
            return MockReply(content=turn.last_tool_result())
        return None

    Brain().on(chat).install(mock_llm)
    run_id = (await client.post("/api/runs", json={"text": "greet Ada", "mode": "chat"})).json()["id"]
    run = await wait_terminal(client, run_id)
    assert run["final_output"] == "hello, Ada!"
    events = sys.modules["jevora_plugin_hello_plugin"].EVENTS
    assert f"before_run:{run_id}" in events and "after_node:chat" in events
    assert any(e.startswith("decision:completion:") for e in events)
    await client.patch("/api/plugins/hello_plugin", json={"enabled": False})
    events.clear()
    run_id = (await client.post("/api/runs", json={"text": "greet again", "mode": "chat"})).json()["id"]
    await wait_terminal(client, run_id)
    assert events == []  # disabled plugins' hooks don't run
    assert (await client.patch("/api/plugins/unknown", json={"enabled": True})).status_code == 404


# --------------------------------------------------------------------------- features / web search
async def test_features_and_web_search_flag(harness: AppHarness) -> None:
    features = (await harness.client.get("/api/features")).json()
    assert features["web_search"] is False and features["oauth"] == {"google": False, "github": False}
    from backend.agents.tools.search import WebSearchUnavailable

    with pytest.raises(WebSearchUnavailable):
        await harness.ctx.web_search.search("anything")
    harness.ctx.settings.web_search_enabled = True
    harness.ctx.settings.searxng_url = "https://searx.test"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"title": "Jevora", "url": "https://jevora.dev",
                                                      "content": "An agent IDE"}]})

    from backend.agents.tools.search import WebSearchService

    service = WebSearchService(harness.ctx.settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    results = await service.search("jevora")
    assert results[0].url == "https://jevora.dev"
    assert (await harness.client.get("/api/features")).json()["web_search"] is True
