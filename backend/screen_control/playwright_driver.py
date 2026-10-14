"""Playwright web driver (default). Runs Chromium headless on a dedicated subprocess-capable loop.

``SCREEN_BROWSER=local`` launches a separate headless browser process (isolated from the coder's
sandbox). ``SCREEN_BROWSER=docker`` connects to a Playwright server container instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from backend.core.logging import get_logger
from backend.core.loop_thread import LoopThread
from backend.decision.schemas import Assertion
from backend.screen_control.driver import Action, AssertionSpec, DomainNotAllowed, ScreenDriver, host_allowed

log = get_logger(__name__)
_LOOP = LoopThread("jevora-playwright")


class PlaywrightDriver(ScreenDriver):
    kind = "playwright"

    def __init__(self, *, allowed_domains: list[str], width: int = 1280, height: int = 800,
                 ws_endpoint: str | None = None, nav_timeout_ms: int = 15_000) -> None:
        self.allowed = allowed_domains
        self.width, self.height = width, height
        self.ws_endpoint = ws_endpoint
        self.nav_timeout_ms = nav_timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._console: list[str] = []
        self._network: list[str] = []
        self._blocked: list[str] = []

    async def _call(self, coro: Any, timeout: float = 60) -> Any:
        return await _LOOP.run(coro, timeout=timeout)

    # ---- coroutines executed on the Playwright loop -------------------------------------------
    async def _start(self, url: str | None) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        if self.ws_endpoint:
            self._browser = await self._pw.chromium.connect(self.ws_endpoint)
        else:
            self._browser = await self._pw.chromium.launch(headless=True, args=["--disable-gpu"])
        context = await self._browser.new_context(viewport={"width": self.width, "height": self.height},
                                                  device_scale_factor=1)
        page = await context.new_page()
        page.set_default_timeout(self.nav_timeout_ms)
        page.on("console", lambda msg: self._console.append(f"{msg.type}: {msg.text}")
                if msg.type in ("error",) else None)
        page.on("pageerror", lambda err: self._console.append(f"pageerror: {err}"))
        page.on("requestfailed", lambda req: self._network.append(f"failed {req.method} {req.url}: "
                                                                   f"{req.failure}"))
        page.on("response", lambda resp: self._network.append(f"HTTP {resp.status} {resp.url}")
                if resp.status >= 400 else None)

        async def guard(route: Any) -> None:
            request = route.request
            if request.is_navigation_request() and not host_allowed(request.url, self.allowed):
                self._blocked.append(request.url)
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await page.route("**/*", guard)
        self._page = page
        if url:
            await page.goto(url, wait_until="domcontentloaded")

    async def _screenshot(self) -> bytes:
        data: bytes = await self._page.screenshot(type="png", full_page=False)
        return data

    async def _perform(self, action: Action) -> str:
        page = self._page
        mouse, keyboard = page.mouse, page.keyboard
        if action.action == "click" and action.x is not None and action.y is not None:
            await mouse.click(action.x, action.y)
        elif action.action == "double_click" and action.x is not None and action.y is not None:
            await mouse.dblclick(action.x, action.y)
        elif action.action == "type":
            if action.x is not None and action.y is not None:
                await mouse.click(action.x, action.y)
            await keyboard.type(action.text or "", delay=10)
        elif action.action == "key":
            await keyboard.press(action.key or action.text or "Enter")
        elif action.action == "scroll":
            if action.x is not None and action.y is not None:
                await mouse.move(action.x, action.y)
            await mouse.wheel(action.dx, action.dy or 400)
        elif action.action == "wait":
            await asyncio.sleep(min(10.0, (action.ms or 1000) / 1000))
        elif action.action == "navigate" and action.url:
            await page.goto(action.url, wait_until="domcontentloaded")
        else:
            return f"ignored {action.action}"
        with contextlib.suppress(Exception):  # best-effort settle after the action
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        await asyncio.sleep(0.15)
        return "ok"

    async def _check(self, spec: AssertionSpec) -> Assertion:
        page = self._page
        try:
            if spec.type == "text_visible":
                visible = await page.get_by_text(spec.value, exact=False).first.is_visible()
                return Assertion(name=spec.name, passed=bool(visible),
                                 detail="" if visible else f"text '{spec.value}' not visible")
            if spec.type == "selector_visible":
                visible = await page.locator(spec.value).first.is_visible()
                return Assertion(name=spec.name, passed=bool(visible), detail="" if visible else "not visible")
            if spec.type == "title_contains":
                title = await page.title()
                return Assertion(name=spec.name, passed=spec.value.lower() in title.lower(), detail=f"title={title!r}")
            if spec.type == "url_contains":
                return Assertion(name=spec.name, passed=spec.value in page.url, detail=f"url={page.url}")
            return Assertion(name=spec.name, passed=not self._console, detail="; ".join(self._console[:3]))
        except Exception as exc:
            return Assertion(name=spec.name, passed=False, detail=f"{type(exc).__name__}: {exc}")

    async def _close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()

    # ---- public API (awaitable from any loop) -------------------------------------------------
    async def start(self, url: str | None) -> None:
        if url and not host_allowed(url, self.allowed):
            raise DomainNotAllowed(f"{url} is outside the screen-control allow-list {self.allowed}")
        await self._call(self._start(url), timeout=90)

    async def screenshot(self) -> bytes:
        data: bytes = await self._call(self._screenshot())
        return data

    async def perform(self, action: Action) -> str:
        if action.action == "navigate" and action.url and not host_allowed(action.url, self.allowed):
            raise DomainNotAllowed(f"navigation to {action.url} is not allowed")
        result: str = await self._call(self._perform(action))
        return result

    async def current_url(self) -> str:
        return str(self._page.url) if self._page is not None else ""

    async def check(self, spec: AssertionSpec) -> Assertion:
        result: Assertion = await self._call(self._check(spec))
        return result

    def console_errors(self) -> list[str]:
        return list(self._console)

    def network_errors(self) -> list[str]:
        return list(self._network) + [f"blocked navigation to {u}" for u in self._blocked]

    async def close(self) -> None:
        try:
            await self._call(self._close(), timeout=30)
        except Exception:
            log.warning("playwright close failed", exc_info=True)
