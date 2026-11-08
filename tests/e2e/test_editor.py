"""E2E: the workspace editor (tabs + save) and the @-mention popover work against the served frontend."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.main import create_app
from tests.conftest import make_settings
from tests.integration.test_screen_control import _chromium_installed
from tests.support.servers import ThreadedServer

pytestmark = [pytest.mark.e2e, pytest.mark.playwright,
              pytest.mark.skipif(not _chromium_installed(), reason="playwright install chromium not run")]


@pytest.fixture
def live_app(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    server = ThreadedServer(create_app(make_settings(tmp_path))).start()
    yield server.url, tmp_path / "workspace"
    server.stop()


def test_editor_open_edit_save_and_mentions(live_app: tuple[str, Path]) -> None:
    from playwright.sync_api import sync_playwright

    base, workspace = live_app
    (workspace / "hello.py").write_text("print('hello')\n", encoding="utf-8")

    console_errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.goto(base + "/index.html", wait_until="networkidle")
        page.wait_for_timeout(1000)

        # Editor: toggle → open hello.py from the tree → edit → save
        page.click("#editorToggleBtn")
        page.wait_for_timeout(500)
        assert page.locator("#editorWorkspace").is_visible()
        page.click(".editor-tree-file")
        page.wait_for_timeout(2500)  # Monaco loads from the CDN; a textarea fallback covers offline
        if page.locator("#editorHost .monaco-editor").count():
            page.click("#editorHost .monaco-editor")
            page.keyboard.press("Control+End")
            page.keyboard.type("\n# touched")
            page.keyboard.press("Control+s")
        else:
            area = page.locator("#editorHost .editor-fallback")
            area.fill(area.input_value() + "\n# touched")
            page.click(".editor-tab-action")  # Save all
        page.wait_for_timeout(1200)
        saved = page.evaluate("fetch('/api/workspace/file?path=hello.py').then(r => r.json())")
        assert "touched" in saved.get("content", ""), "editor save did not persist"

        # Mentions: typing @ opens the workspace file picker; Enter inserts the path
        page.click("#editorToggleBtn")  # back to chat
        page.click("#agentInput")
        page.type("#agentInput", "please fix @")
        page.wait_for_timeout(800)
        assert page.locator("#mentionPopover").is_visible(), "mention popover did not open"
        page.keyboard.press("Enter")
        assert "@hello.py" in page.input_value("#agentInput")
        browser.close()

    assert not console_errors, console_errors
