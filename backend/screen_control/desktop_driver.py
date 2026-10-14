"""OS-level desktop automation (pyautogui), behind ``DESKTOP_CONTROL_ENABLED=true``.

This drives the real mouse and keyboard of the machine running the backend, so it is off by
default and must be explicitly enabled. Assertions that need a DOM are reported as unsupported.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from backend.decision.schemas import Assertion
from backend.screen_control.driver import Action, AssertionSpec, ScreenDriver


class DesktopControlDisabled(Exception):
    pass


class DesktopDriver(ScreenDriver):
    kind = "desktop"

    def __init__(self, enabled: bool) -> None:
        if not enabled:
            raise DesktopControlDisabled("desktop control is disabled (set DESKTOP_CONTROL_ENABLED=true)")
        try:
            import pyautogui
        except ImportError as exc:
            raise DesktopControlDisabled("pyautogui is not installed (pip install pyautogui)") from exc
        self._gui: Any = pyautogui
        self._gui.FAILSAFE = True  # moving the mouse to a corner aborts
        self.width, self.height = self._gui.size()

    async def start(self, url: str | None) -> None:
        return None

    async def screenshot(self) -> bytes:
        def grab() -> bytes:
            image = self._gui.screenshot()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        return await asyncio.to_thread(grab)

    async def perform(self, action: Action) -> str:
        gui = self._gui

        def act() -> str:
            if action.action in ("click", "double_click") and action.x is not None and action.y is not None:
                gui.click(action.x, action.y, clicks=2 if action.action == "double_click" else 1)
            elif action.action == "type":
                if action.x is not None and action.y is not None:
                    gui.click(action.x, action.y)
                gui.write(action.text or "", interval=0.01)
            elif action.action == "key":
                keys = (action.key or action.text or "enter").lower().split("+")
                gui.hotkey(*keys)
            elif action.action == "scroll":
                gui.scroll(-(action.dy or 400))
            elif action.action == "wait":
                import time

                time.sleep(min(10.0, (action.ms or 1000) / 1000))
            else:
                return f"ignored {action.action}"
            return "ok"

        return await asyncio.to_thread(act)

    async def current_url(self) -> str:
        return ""

    async def check(self, spec: AssertionSpec) -> Assertion:
        return Assertion(name=spec.name, passed=False, detail="assertion not supported by the desktop driver")

    def console_errors(self) -> list[str]:
        return []

    def network_errors(self) -> list[str]:
        return []

    async def close(self) -> None:
        return None
