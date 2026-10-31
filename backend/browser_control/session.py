"""Browser session lifecycle manager.

Binds browser-control sessions to users and (optionally) VMs, tracks them in the
``browser_sessions`` table, and dispatches to the appropriate driver (DCOM/COM,
desktop capture, Playwright) based on the session ``kind``. This is the integration
point that makes browser control first-class in the VM workflow: a user can attach a
browser-control session to an existing sandbox/VM so the agent can observe and drive
a browser inside that environment.

Lifecycle: create -> start -> (actions/assertions) -> stop/delete.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.browser_control.driver import (
    BrowserControlError,
    ComBrowserDriver,
    DesktopCaptureDriver,
    DomainNotAllowed,
    PlaywrightBrowserDriver,
    ScreenDriver,
    host_allowed,
)
from backend.browser_control.models import (
    AssertionSpec,
    BrowserAction,
    BrowserActionResult,
    BrowserControlState,
    BrowserScreenshot,
    BrowserSessionCreate,
    BrowserSessionInfo,
    BrowserSessionUpdate,
)
from backend.config import Settings
from backend.core.errors import AppError, NotFound
from backend.core.logging import get_logger
from backend.db.models import VM, BrowserSession

log = get_logger(__name__)


class SessionManager:
    """Per-process singleton of running browser sessions + persistence layer."""
    _instances: ClassVar[dict[str, SessionManager]] = {}

    def __new__(cls, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> SessionManager:
        key = f"{settings.database_url}-{settings.data_dir}"
        if key not in cls._instances:
            cls._instances[key] = super().__new__(cls)
        return cls._instances[key]

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        if hasattr(self, "_initialized"):
            return
        self.settings = settings
        self.sessions = sessions
        self._sessions: dict[str, BrowserSessionInfo] = {}
        self._drivers: dict[str, ScreenDriver] = {}
        self._states: dict[str, BrowserControlState] = {}
        self._lock = asyncio.Lock()
        self._initialized = True

    # -------------------------------------------------- CRUD over persisted sessions
    async def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.scalars(select(BrowserSession).where(BrowserSession.user_id == user_id)
                                         .order_by(BrowserSession.created_at.desc()))
        return [BrowserSessionInfo.from_row(r).model_dump() for r in rows]

    async def create_session(self, user_id: str, body: BrowserSessionCreate) -> BrowserSessionInfo:
        kind = body.kind
        if not self.settings.browser_control_enabled:
            raise AppError("browser control is disabled (set BROWSER_CONTROL_ENABLED=true)", code="feature_disabled")
        if kind == "dcom_ie" and not self.settings.browser_dcom_enabled:
            raise AppError("DCOM browser control is disabled", code="feature_disabled")
        if kind == "desktop" and not self.settings.browser_desktop_capture_enabled:
            raise AppError("desktop capture is disabled", code="feature_disabled")
        allowed = body.allowed_hosts or self.settings.browser_allowed_hosts
        async with self.sessions() as session:
            session.add(BrowserSession(
                user_id=user_id,
                name=body.name[:120],
                kind=kind,
                status="stopped",
                browser_path=None,
                clsid=None,
                pid=None,
                host=None,
                ws_endpoint=None,
                headless=body.headless,
                viewport_width=body.width,
                viewport_height=body.height,
                allowed_hosts=allowed,
                config=body.config,
            ))
            await session.commit()
        # fetch the newly added row by user_id + latest created_at
        async with self.sessions() as session:
            row = await session.scalar(select(BrowserSession).where(BrowserSession.user_id == user_id)
                                       .order_by(BrowserSession.created_at.desc()).limit(1))
        return BrowserSessionInfo.from_row(row)

    async def get_sessions(self, user_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.scalars(select(BrowserSession).where(BrowserSession.user_id == user_id)
                                         .order_by(BrowserSession.status.desc(), BrowserSession.created_at.desc()))
        return [BrowserSessionInfo.from_row(r).model_dump() for r in rows]

    async def get_session_by_id(self, user_id: str, session_id: str) -> BrowserSessionInfo | None:
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
        if row is None or row.user_id != user_id:
            return None
        return BrowserSessionInfo.from_row(row)

    async def update_session(self, user_id: str, session_id: str, body: BrowserSessionUpdate) -> BrowserSessionInfo:
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
        if row is None or row.user_id != user_id:
            raise NotFound("browser session not found")
        for field in ("name", "headless"):
            val = getattr(body, field)
            if val is not None:
                setattr(row, field, val)
        if body.width is not None:
            row.viewport_width = body.width
        if body.height is not None:
            row.viewport_height = body.height
        if body.config is not None:
            row.config = body.config
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return BrowserSessionInfo.from_row(row)

    async def delete_session(self, user_id: str, session_id: str) -> None:
        await self.stop_session_internal(session_id)
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
        if row is None or row.user_id != user_id:
            raise NotFound("browser session not found")
        async with self.sessions() as session:
            await session.delete(row)
            await session.commit()

    async def get_active(self, user_id: str, vm_id: str | None = None) -> BrowserSessionInfo | None:
        async with self.sessions() as session:
            rows = await session.scalars(select(BrowserSession).where(
                BrowserSession.user_id == user_id, BrowserSession.status == "running"
            ).order_by(BrowserSession.created_at.desc()))
        for r in rows:
            if vm_id is None or r.vm_id == vm_id:
                return BrowserSessionInfo.from_row(r)
        return None

    # -------------------------------------------------- lifecycle
    async def start_session(self, user_id: str, session_id: str, url: str | None = None,
                            vm_id: str | None = None) -> BrowserSessionInfo:
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
            if row is None or row.user_id != user_id:
                raise NotFound("browser session not found")
            if url and not host_allowed(url, row.allowed_hosts):
                raise DomainNotAllowed(f"{url} is outside the session allow-list {row.allowed_hosts}")
            if vm_id:
                # VM lookup must happen while the session is still open.
                vm = await session.get(VM, vm_id)
                if vm is None or vm.user_id != user_id:
                    raise NotFound("VM not found")
                row.vm_id = vm_id
        await self._start_driver(row, url)
        row.status = "running"
        row.started_at = datetime.now(UTC)
        row.last_activity_at = row.started_at
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return BrowserSessionInfo.from_row(row)

    async def stop_session(self, user_id: str, session_id: str) -> BrowserSessionInfo:
        await self.stop_session_internal(session_id)
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
            if row is None or row.user_id != user_id:
                raise NotFound("browser session not found")
            row.status = "stopped"
            row.last_activity_at = datetime.now(UTC)
            await session.commit()
            return BrowserSessionInfo.from_row(row)

    async def stop_session_internal(self, session_id: str) -> None:
        driver = self._drivers.pop(session_id, None)
        if driver is not None:
            try:
                await driver.close()
            except Exception:
                log.exception("browser session driver close failed")
        self._states.pop(session_id, None)

    async def shutdown(self) -> None:
        """Stop every running driver (invoked on app shutdown)."""
        for sid in list(self._drivers.keys()):
            await self.stop_session_internal(sid)

    # -------------------------------------------------- actions + assertions
    async def perform_action(self, user_id: str, session_id: str, action: BrowserAction) -> BrowserActionResult:
        row = await self.get_session_by_id(user_id, session_id)
        if row is None:
            raise NotFound("browser session not found")
        driver = self._drivers.get(session_id)
        if driver is None:
            return BrowserActionResult(ok=False, message="session not running", data={"status": row.status})
        # Navigation is gated by the drivers themselves; a DomainNotAllowed raised
        # there is converted into a clean blocked result by the except clause below.
        try:
            out = await driver.perform(action)
            url = await driver.current_url()
            await self._touch(session_id)
            driver_state = self._states.get(session_id) or BrowserControlState(url="", title="",
                                                                                console_errors=[], network_errors=[])
            if driver_state is not None:
                driver_state.url = url
                driver_state.console_errors = driver.console_errors()
                driver_state.network_errors = driver.network_errors()
                driver_state.action_log.append({"action": action.model_dump(exclude_none=True), "result": out})
            return BrowserActionResult(ok=True, message=out, url=url, data={})
        except DomainNotAllowed as exc:
            return BrowserActionResult(ok=False, message=f"blocked: {exc}")
        except BrowserControlError as exc:
            return BrowserActionResult(ok=False, message=f"browser control error: {exc}")
        except Exception as exc:
            return BrowserActionResult(ok=False, message=f"unexpected error: {type(exc).__name__}: {exc}")

    async def _touch(self, session_id: str) -> None:
        """Persist last_activity_at without detaching the caller's row copy."""
        async with self.sessions() as session:
            row = await session.get(BrowserSession, session_id)
            if row is not None:
                row.last_activity_at = datetime.now(UTC)
                await session.commit()

    async def take_screenshot(self, user_id: str, session_id: str) -> BrowserScreenshot:
        row = await self.get_session_by_id(user_id, session_id)
        if row is None:
            raise NotFound("browser session not found")
        driver = self._drivers.get(session_id)
        if driver is None:
            raise BrowserControlError("session not running")
        png = await driver.screenshot()
        b64 = base64.b64encode(png).decode("ascii")
        await self._touch(session_id)
        return BrowserScreenshot(kind=driver.kind, width=row.viewport_width, height=row.viewport_height,
                                 b64_png=b64, timestamp=datetime.now(UTC))

    async def check_assertion(self, user_id: str, session_id: str, spec: AssertionSpec) -> dict[str, Any]:
        row = await self.get_session_by_id(user_id, session_id)
        if row is None:
            raise NotFound("browser session not found")
        driver = self._drivers.get(session_id)
        if driver is None:
            return {"name": spec.name, "passed": False, "detail": "session not running"}
        assertion = await driver.check(spec)
        return {"name": assertion.name, "passed": assertion.passed, "detail": assertion.detail}

    # -------------------------------------------------- driver dispatch
    async def _start_driver(self, row: BrowserSession, url: str | None) -> None:
        kind = row.kind
        width, height = row.viewport_width, row.viewport_height
        allowed = row.allowed_hosts
        if kind == "dcom_ie":
            driver = ComBrowserDriver(
                clsid=self.settings.browser_ie_clsid,
                allowed_hosts=allowed,
                width=width,
                height=height,
                timeout_s=self.settings.browser_default_timeout_s,
                headless=not self.settings.browser_desktop_capture_enabled,
                browser_path=self.settings.browser_chrome_path,
            )
        elif kind == "dcom_chrome":
            driver = ComBrowserDriver(
                clsid=self.settings.browser_chrome_clsid or "",
                allowed_hosts=allowed,
                width=width,
                height=height,
                timeout_s=self.settings.browser_default_timeout_s,
                headless=True,
                browser_path=self.settings.browser_chrome_path,
            )
        elif kind == "desktop":
            driver = DesktopCaptureDriver(width=width, height=height)
        else:
            driver = PlaywrightBrowserDriver(allowed_hosts=allowed, width=width, height=height)
        await driver.start(url)
        row.status = "running"
        row.pid = getattr(driver, "_pid", None)
        row.browser_path = getattr(driver, "browser_path", None)
        self._drivers[row.id] = driver
        self._states[row.id] = BrowserControlState(url=await driver.current_url(), title="",
                                                   console_errors=driver.console_errors(),
                                                   network_errors=driver.network_errors())

    def get_state(self, session_id: str) -> BrowserControlState | None:
        return self._states.get(session_id)
