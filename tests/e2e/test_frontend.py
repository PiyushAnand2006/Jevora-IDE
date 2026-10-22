"""MVP criterion 6: the real frontend, served by the backend at /, completes a Build run with no console
errors, shows the decision + confidence line, and the provider flow (add → list models → select) works."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.main import create_app
from tests.conftest import make_settings
from tests.integration.test_screen_control import _chromium_installed
from tests.support.scenarios import build_brain
from tests.support.servers import MockLLM, ThreadedServer

pytestmark = [pytest.mark.e2e, pytest.mark.playwright,
              pytest.mark.skipif(not _chromium_installed(), reason="playwright install chromium not run")]


@pytest.fixture
def live_app(tmp_path: Path) -> Iterator[str]:
    server = ThreadedServer(create_app(make_settings(tmp_path))).start()
    yield server.url
    server.stop()


def test_frontend_build_run_and_provider_flow(live_app: str, mock_llm_session: MockLLM) -> None:
    from playwright.sync_api import sync_playwright

    mock_llm_session.models = ["mock-mini", "mock-pro"]
    mock_llm_session.required_key = None
    brain, _ = build_brain()
    brain.install(mock_llm_session)
    console_errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(f"pageerror: {err}"))
        page.goto(live_app + "/", wait_until="networkidle")
        assert page.title() == "Jevora IDE"

        # ---- provider flow: add custom provider → list models → select one
        page.click("#currentModelBadge")
        page.click("#addProviderBtn")
        page.click("#customProviderBtn")
        page.fill("#providerNameInput", "Mock Server")
        page.fill("#providerBaseUrlInput", mock_llm_session.base_url)
        page.fill("#providerApiKeyInput", "sk-test-frontend-key-123")
        page.click("#providerSaveBtn")
        page.wait_for_selector(".saved-provider-item .sp-name:text('Mock Server')")
        page.click(".saved-provider-item:has-text('Mock Server')")
        page.wait_for_selector(".provider-model-item:text('mock-pro')")
        assert sorted(page.locator(".provider-model-item").all_inner_texts()) == ["mock-mini", "mock-pro"]
        page.click(".provider-model-item:text('mock-pro')")
        assert "mock-pro" in page.inner_text("#currentModelBadge")
        stored: Any = page.evaluate("localStorage.getItem('jevora_providers')")
        assert "sk-test-frontend-key" not in stored  # keys are never kept in the browser
        # back to Auto so the Decision Engine routes models by task type
        page.click("#currentModelBadge")
        page.click(".model-list-item[data-model='auto']")

        # ---- Build-mode run, rendered live
        page.fill("#agentInput", "Create calc.py with add(a, b) and pytest tests")
        page.click("#sendBtn")
        page.wait_for_selector(".message-bubble.agent .run-status-chip.done", timeout=90_000)
        line = page.inner_text(".message-bubble.agent .decision-line")
        assert re.search(r"Decision engine: confirm_done \(\d+% confidence\) · done", line), line
        tree_roles = page.locator(".message-bubble.agent .tree-node .tn-role").all_text_contents()
        assert {"router", "planner", "coder"} <= set(tree_roles)
        assert "2 passed" in page.inner_text(".message-bubble.agent .final-output")

        # ---- drawers render live data without errors
        page.click(".tool-row[data-tool='files']")
        page.wait_for_selector(".file-tree .ft-item[data-path='calc.py']")
        page.click(".file-tree .ft-item[data-path='calc.py']")
        page.wait_for_selector("#fileViewer pre")
        assert "def add" in page.inner_text("#fileViewer pre")
        page.click("#drawerCloseBtn")
        page.click(".tool-row[data-tool='mcp']")
        page.wait_for_selector("#mcpServers")
        assert "Decision & Verifier Engine" in page.inner_text("#drawerBody")
        page.click("#drawerCloseBtn")
        page.click(".tool-row[data-tool='plugins']")
        page.wait_for_selector("input.toggle-input[data-id='iterative_tester']")
        page.click("#drawerCloseBtn")
        page.click("#historyBtn")
        page.wait_for_selector("#historyList .conversation-row")
        assert "Create calc.py" in page.inner_text("#historyList")
        page.click("#historyList .conversation-row")
        page.wait_for_selector("#conversationContext")
        assert "Create calc.py" in page.inner_text("#streamContainer .message-bubble.user")
        assert "2 passed" in page.inner_text("#streamContainer .message-bubble.agent")
        assert page.locator("#continueConversationBtn").is_enabled()
        page.click("#vmPill")
        page.wait_for_selector("#vmCreate")
        page.click("#drawerCloseBtn")
        assert page.inner_text("#agentCountBadge") == "0"
        browser.close()
    assert console_errors == [], console_errors
