"""PRD MVP success criteria 1–5 as explicit acceptance tests (criterion 6 lives in test_frontend.py)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from backend.db.models import AgentCall, Decision
from tests.conftest import AppHarness, add_mock_provider, make_settings, start_app
from tests.integration.test_docker_sandbox import _docker_ok
from tests.integration.test_runs_api import wait_terminal
from tests.support.brain import Brain, Turn, call, tools_reply
from tests.support.scenarios import build_brain, critic_rule, planner_rule, router_rule
from tests.support.servers import MockLLM, MockReply

pytestmark = pytest.mark.e2e

FLASK_APP = 'from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route("/")\ndef hello():\n    return "Hello, World!"\n'
FLASK_TEST = ("from app import app\n\n\ndef test_hello():\n    client = app.test_client()\n"
              "    response = client.get('/')\n    assert response.status_code == 200\n"
              "    assert response.data == b'Hello, World!'\n")


@pytest.fixture
async def docker_app(tmp_path: Path) -> AsyncIterator[AppHarness]:
    settings = make_settings(tmp_path, sandbox_mode="docker", local_unsafe_exec=False)
    app, lifespan = await start_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield AppHarness(app, client)
    await lifespan.__aexit__(None, None, None)


def flask_brain() -> Brain:
    def coder(turn: Turn) -> MockReply | None:
        if turn.role != "coder":
            return None
        if turn.step == 0:
            return tools_reply(call("write_file", path="app.py", content=FLASK_APP),
                               call("write_file", path="test_app.py", content=FLASK_TEST))
        if turn.step == 1:
            return tools_reply(call("run_command", command="python -c \"import flask, sys; print(sys.version)\""))
        if turn.step == 2:
            return tools_reply(call("run_tests"))
        return MockReply(content="Created a Flask hello-world app with a passing pytest.")

    spec = {"title": "flask hello", "intent": "build", "needs_code_changes": True, "needs_tests": True,
            "ui_work": False, "keywords": ["flask", "pytest"], "risk": "low"}
    return Brain().on(router_rule(spec)).on(planner_rule).on(coder).on(critic_rule)


@pytest.mark.docker
@pytest.mark.skipif(not _docker_ok(), reason="Docker daemon not available")
async def test_criterion_1_coder_builds_flask_app_in_docker_sandbox(docker_app: AppHarness, mock_llm: MockLLM) -> None:
    """Plan → real file + terminal tools in the Docker sandbox → Decision Engine verifies from test evidence."""
    status = await docker_app.ctx.sandboxes.status()
    if not status["available"]:
        pytest.skip(status["reason"])
    await add_mock_provider(docker_app.client, mock_llm)
    flask_brain().install(mock_llm)
    run_id = (await docker_app.client.post("/api/runs", json={
        "text": "Create a Flask hello-world with a passing pytest", "mode": "build"})).json()["id"]
    run = await wait_terminal(docker_app.client, run_id, timeout=300)
    assert run["status"] == "done", run["final_output"]
    assert run["decisions"][-1]["choice"] == "confirm_done"
    events = docker_app.ctx.events.history(run_id)
    terminal_lines = "".join(e.data.get("data", "") for e in events if e.type == "terminal.output")
    assert "3.11" in terminal_lines  # the command really ran in the python:3.11 sandbox image
    tests = [e for e in events if e.type == "test.result"]
    assert tests and tests[-1].data["ok"] and tests[-1].data["passed"] == 1
    async with docker_app.ctx.sessions() as session:
        verdict = await session.scalar(select(Decision).where(Decision.run_id == run_id, Decision.acted,
                                                              Decision.type == "verification"))
    evidence_kinds = {item["kind"] for item in verdict.input_state_json["evidence"]}
    assert {"test_result", "diff"} <= evidence_kinds  # verified from evidence, not the agent's claim
    assert (docker_app.ctx.settings.workspace_root / "app.py").exists()


async def test_criterion_2_two_model_tiers_in_one_run(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    build_brain()[0].install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=90)
    assert run["status"] == "done"
    async with harness.ctx.sessions() as session:
        calls = list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)))
        routing = list(await session.scalars(select(Decision).where(Decision.run_id == run_id,
                                                                    Decision.type == "routing")))
    tiers = {c.category: c.model_used for c in calls if c.model_used}
    assert tiers["fast_classification"] == "mock-mini" and tiers["heavy_reasoning"] == "mock-pro"
    assert routing and routing[0].choice == "code_generation"


async def test_criterion_3_vault_history_reads_back_identically(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    build_brain()[0].install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    await wait_terminal(harness.client, run_id, timeout=90)
    for _ in range(100):
        if (await harness.client.get(f"/api/runs/{run_id}")).json()["vault_note_path"]:
            break
        await asyncio.sleep(0.05)
    api_tree = (await harness.client.get(f"/api/runs/{run_id}/tree")).json()
    vault = (await harness.client.get(f"/api/vault/runs/{run_id}")).json()
    assert vault["tree"] == api_tree
    note = (harness.ctx.vault.path / (await harness.client.get(f"/api/runs/{run_id}")).json()["vault_note_path"])
    text = note.read_text(encoding="utf-8")
    assert "[[Jevora/Calls/" in text and "confirm_done" in text


async def test_criterion_4_wrong_output_claiming_success_is_rejected(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    harness.ctx.settings.iteration_ceiling = 2
    build_brain(buggy=99)[0].install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={"text": "Create calc.py", "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=90)
    async with harness.ctx.sessions() as session:
        verdicts = list(await session.scalars(select(Decision).where(
            Decision.run_id == run_id, Decision.type == "verification", Decision.acted)))
    assert verdicts and {d.choice for d in verdicts} <= {"restart_same_agent", "reassign_new_agent", "escalate"}
    assert run["status"] != "done"


async def test_criterion_5_screen_agent_evidence_judged_by_engine(harness: AppHarness, mock_llm: MockLLM) -> None:
    from tests.integration.test_screen_control import PAGE, _chromium_installed, screen_brain

    if not _chromium_installed():
        pytest.skip("playwright install chromium not run")
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from tests.support.servers import ThreadedServer

    async def page(_: object) -> HTMLResponse:
        return HTMLResponse(PAGE)

    server = ThreadedServer(Starlette(routes=[Route("/", page)])).start()
    try:
        mock_llm.models = ["mock-mini", "mock-pro", "mock-vision"]
        await add_mock_provider(harness.client, mock_llm)
        brain = screen_brain(click=True)
        brain.on(lambda t: MockReply(content=json.dumps({"title": "t", "intent": "other", "needs_code_changes": False,
                                                         "needs_tests": False, "ui_work": False, "keywords": [],
                                                         "risk": "low"})) if t.role == "router" else None)
        brain.install(mock_llm)
        run_id = (await harness.client.post("/api/runs", json={
            "text": f'/test {server.url}/ expect "Clicked!"', "mode": "build"})).json()["id"]
        run = await wait_terminal(harness.client, run_id, timeout=120)
        assert run["status"] == "done"
        async with harness.ctx.sessions() as session:
            verdict = await session.scalar(select(Decision).where(Decision.run_id == run_id, Decision.acted,
                                                                  Decision.type == "verification"))
        screen = [i for i in verdict.input_state_json["evidence"] if i["kind"] == "screenshot"]
        assert screen and all(a["passed"] for a in screen[0]["payload"]["assertions"])
        assert verdict.choice == "confirm_done"
    finally:
        server.stop()


async def test_auth_flow_in_browser(tmp_path: Path) -> None:
    from tests.integration.test_screen_control import _chromium_installed

    if not _chromium_installed():
        pytest.skip("playwright install chromium not run")
    from backend.main import create_app
    from tests.support.servers import ThreadedServer

    server = ThreadedServer(create_app(make_settings(tmp_path, auth_required=True))).start()

    def drive() -> list[str]:
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(server.url + "/index.html", wait_until="networkidle")
            page.wait_for_url("**/auth.html")  # AUTH_REQUIRED=true sends anonymous users to sign in
            page.click("#githubBtn")
            page.wait_for_selector("#authMessage:not([hidden])")
            assert "GitHub sign-in is not configured" in page.inner_text("#authMessage")
            page.click("#switchModeBtn")
            page.fill("#nameInput", "Ada Lovelace")
            page.fill("#emailInput", "ada@example.com")
            page.fill("#passwordInput", "analytical-engine")
            page.click("#submitBtn")
            page.wait_for_url("**/index.html")
            page.wait_for_selector("#profileChip:not([hidden])")
            assert page.inner_text("#profileAvatar") == "AL"
            assert "ada@example.com" in page.inner_text(".sidebar-user")
            page.click("#signOutBtn")
            page.wait_for_url("**/auth.html")
            page.fill("#emailInput", "ada@example.com")
            page.fill("#passwordInput", "wrong-password")
            page.click("#submitBtn")
            page.wait_for_selector("#authMessage.error")
            assert "invalid email or password" in page.inner_text("#authMessage").lower()
            browser.close()
        return errors

    try:
        errors = await asyncio.to_thread(drive)
        assert errors == []
    finally:
        server.stop()
