"""DesktopDriver tests.

Two tiers:
* fake-pyautogui unit tests (always run) covering gating and action dispatch;
* live desktop tests that skip gracefully when no interactive desktop is
  available (CI containers, this repo's own sandboxed shells).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from backend.screen_control.desktop_driver import DesktopControlDisabled, DesktopDriver
from backend.screen_control.driver import Action, AssertionSpec


class FakeGui:
    """Stand-in for the pyautogui module, recording every call."""

    def __init__(self) -> None:
        self.FAILSAFE = False
        self.size_value = (1280, 800)
        self.calls: list[tuple] = []

    def size(self):
        return self.size_value

    def click(self, x, y, clicks=1):
        self.calls.append(("click", x, y, clicks))

    def write(self, text, interval=0.0):
        self.calls.append(("write", text, interval))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))

    def screenshot(self):  # minimal PIL-image-like object

        import PIL.Image
        return PIL.Image.new("RGB", (32, 16), (10, 20, 30))


@pytest.fixture
def fake_gui(monkeypatch) -> FakeGui:
    gui = FakeGui()
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    return gui


def test_disabled_raises():
    with pytest.raises(DesktopControlDisabled):
        DesktopDriver(enabled=False)


def test_missing_pyautogui_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # import -> ImportError
    with pytest.raises(DesktopControlDisabled):
        DesktopDriver(enabled=True)


def test_init_sets_failsafe_and_size(fake_gui):
    driver = DesktopDriver(enabled=True)
    assert driver._gui.FAILSAFE is True
    assert driver.width == 1280 and driver.height == 800


def test_perform_click_and_double_click(fake_gui):
    driver = DesktopDriver(enabled=True)
    assert asyncio.run(driver.perform(Action(action="click", x=10, y=20))) == "ok"
    assert asyncio.run(driver.perform(Action(action="double_click", x=1, y=2))) == "ok"
    assert fake_gui.calls == [("click", 10, 20, 1), ("click", 1, 2, 2)]


def test_perform_type_without_click_position(fake_gui):
    driver = DesktopDriver(enabled=True)
    assert asyncio.run(driver.perform(Action(action="type", text="hi"))) == "ok"
    assert fake_gui.calls == [("write", "hi", 0.01)]


def test_perform_type_clicks_first_when_position_given(fake_gui):
    driver = DesktopDriver(enabled=True)
    asyncio.run(driver.perform(Action(action="type", text="hi", x=5, y=6)))
    assert fake_gui.calls == [("click", 5, 6, 1), ("write", "hi", 0.01)]


def test_perform_key_combination(fake_gui):
    driver = DesktopDriver(enabled=True)
    asyncio.run(driver.perform(Action(action="key", key="Ctrl+S")))
    assert fake_gui.calls == [("hotkey", ("ctrl", "s"))]


def test_perform_scroll_uses_negative_dy(fake_gui):
    driver = DesktopDriver(enabled=True)
    asyncio.run(driver.perform(Action(action="scroll", dy=300)))
    assert fake_gui.calls == [("scroll", -300)]


def test_perform_unknown_action_is_ignored(fake_gui):
    driver = DesktopDriver(enabled=True)
    assert asyncio.run(driver.perform(Action(action="navigate", url="http://x"))) == "ignored navigate"
    assert fake_gui.calls == []


def test_screenshot_png_bytes(fake_gui):
    driver = DesktopDriver(enabled=True)
    data = asyncio.run(driver.screenshot())
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_assertion_unsupported(fake_gui):
    driver = DesktopDriver(enabled=True)
    result = asyncio.run(driver.check(AssertionSpec(type="text_visible", value="x")))
    assert result.passed is False


@pytest.mark.parametrize("has_desktop", [True])
def test_live_desktop_screenshot(has_desktop: bool):
    """Real-screen smoke test: skipped when no interactive desktop exists."""
    try:
        import pyautogui
        pyautogui.size()
    except Exception:
        pytest.skip("no interactive desktop available for pyautogui")

    driver = DesktopDriver(enabled=True)
    data = asyncio.run(driver.screenshot())
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
