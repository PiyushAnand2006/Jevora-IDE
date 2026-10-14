"""Screen-control agent driving real Playwright/Chromium against a local test page (MVP criterion 5)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

from backend.decision.schemas import Evidence
from backend.screen_control.driver import AssertionSpec
from tests.conftest import AppHarness, add_mock_provider
from tests.integration.test_runs_api import wait_terminal
from tests.support.brain import Brain, Turn
from tests.support.runctx import make_run_context
from tests.support.servers import MockLLM, MockReply, ThreadedServer

PAGE = """<!doctype html><html><head><title>Jevora Test App</title>
<style>body{margin:0;font-family:sans-serif} #go{position:absolute;left:100px;top:100px;width:200px;height:60px}
#msg{position:absolute;left:100px;top:200px;font-size:24px}</style></head>
<body><button id="go" onclick="document.getElementById('msg').textContent='Clicked!'">Click me</button>
<p id="msg"></p></body></html>"""


def _chromium_installed() -> bool:
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData" / "Local" / "ms-playwright")
    if not base.exists():
        base = Path.home() / ".cache" / "ms-playwright"
    return base.exists() and any(p.name.startswith("chromium") for p in base.iterdir())


pytestmark = [pytest.mark.playwright,
              pytest.mark.skipif(not _chromium_installed(), reason="playwright install chromium not run")]


@pytest.fixture(scope="module")
def page_server() -> Iterator[str]:
    async def page(_: object) -> HTMLResponse:
        return HTMLResponse(PAGE)

    server = ThreadedServer(Starlette(routes=[Route("/", page)])).start()
    yield server.url + "/"
    server.stop()


def screen_brain(click: bool) -> Brain:
    def rule(turn: Turn) -> MockReply | None:
        if "You control a web browser" not in turn.system:
            return None
        clicked = "click" in json.dumps(turn.messages[-1])  # previous actions are listed in the prompt
        if click and not clicked:
            return MockReply(content='{"action": "click", "x": 200, "y": 130, "reason": "press the button"}')
        return MockReply(content='{"action": "done", "reason": "goal reached"}')

    return Brain().on(rule)


async def _vision_provider(harness: AppHarness, mock_llm: MockLLM) -> None:
    mock_llm.models = ["mock-mini", "mock-pro", "mock-vision"]
    await add_mock_provider(harness.client, mock_llm)


async def test_screen_agent_clicks_and_produces_evidence(harness: AppHarness, mock_llm: MockLLM,
                                                         page_server: str) -> None:
    await _vision_provider(harness, mock_llm)
    screen_brain(click=True).install(mock_llm)
    rc = await make_run_context(harness.ctx, task="click the button")
    result = await harness.ctx.screen.agent.run(rc, goal="Click the button and confirm the message",
                                                url=page_server,
                                                assertions=[AssertionSpec(type="text_visible", value="Clicked!"),
                                                            AssertionSpec(type="title_contains", value="Jevora")])
    payload = result.payload
    assert payload.steps == 1 and payload.stopped_reason.startswith("model finished")
    assert [a.passed for a in payload.assertions] == [True, True, True]  # + no_console_errors
    assert len(payload.screenshots) == 3  # step0, step1, final
    for shot in payload.screenshots:
        assert (harness.ctx.settings.data_dir / shot["path"]).read_bytes().startswith(b"\x89PNG")
    assert payload.action_log[0]["action"] == {"action": "click", "x": 200, "y": 130, "dx": 0, "dy": 0,
                                               "reason": "press the button"}
    verdict = await harness.ctx.decisions.verify_work(
        {"mode": "build", "iteration": 0, "max_iterations": 6, "requires_tests": False, "requires_changes": False},
        Evidence(items=[result.evidence]), recorder=rc.recorder)
    assert verdict.result.choice == "confirm_done"
    sent = [r for r in mock_llm.requests if r["model"] == "mock-vision"]
    assert sent and any(p.get("type") == "image_url" for p in sent[0]["messages"][1]["content"])


async def test_screen_agent_failure_is_not_confirmed(harness: AppHarness, mock_llm: MockLLM, page_server: str) -> None:
    await _vision_provider(harness, mock_llm)
    screen_brain(click=False).install(mock_llm)  # gives up without clicking, "claims" the goal is reached
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.screen.agent.run(rc, goal="Click the button", url=page_server,
                                                assertions=[AssertionSpec(type="text_visible", value="Clicked!")])
    assert result.payload.assertions[0].passed is False
    verdict = await harness.ctx.decisions.verify_work(
        {"mode": "build", "iteration": 0, "max_iterations": 6, "requires_tests": False, "requires_changes": False},
        Evidence(items=[result.evidence]), recorder=rc.recorder)
    assert verdict.result.choice == "restart_same_agent"


async def test_domain_allow_list_blocks_external_sites(harness: AppHarness, mock_llm: MockLLM) -> None:
    await _vision_provider(harness, mock_llm)
    rc = await make_run_context(harness.ctx)
    result = await harness.ctx.screen.agent.run(rc, goal="go", url="https://example.com/", assertions=[])
    assert result.payload.stopped_reason.startswith("blocked") and result.payload.steps == 0


async def test_slash_test_command_runs_screen_agent(harness: AppHarness, mock_llm: MockLLM, page_server: str) -> None:
    await _vision_provider(harness, mock_llm)
    brain = screen_brain(click=True)
    brain.on(lambda turn: MockReply(content='{"title":"t","intent":"other","needs_code_changes":false,'
                                            '"needs_tests":false,"ui_work":false,"keywords":[],"risk":"low"}')
             if turn.role == "router" else None)
    brain.install(mock_llm)
    run_id = (await harness.client.post("/api/runs", json={
        "text": f'/test {page_server} expect "Clicked!"', "mode": "build"})).json()["id"]
    run = await wait_terminal(harness.client, run_id, timeout=120)
    assert run["status"] == "done", run["final_output"]
    assert "Screen test: 2/2 assertions passed" in run["final_output"]
    kinds = {a["kind"] for a in run["artifacts"]}
    assert "screenshot" in kinds
    tree = (await harness.client.get(f"/api/runs/{run_id}/tree")).json()
    assert any(n["role"] == "screen-tester" and n["model_used"] == "mock-vision" for n in tree["roots"])


