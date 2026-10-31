"""Integration-style tests for the browser-control subsystem.

These exercise the validation, schema, and manager logic without starting real
browsers or connecting to a database. The DCOM/COM and desktop-capture drivers
are Windows-specific and are tested via the live UI flow (browser_control tool).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import ClassVar

import pytest
from pydantic import ValidationError

from backend.browser_control.driver import host_allowed
from backend.browser_control.models import (
    AssertionSpec,
    BrowserAction,
    BrowserScreenshot,
    BrowserSessionCreate,
    BrowserSessionInfo,
)

FAKE_SQLALCHEMY_ROW = type(
    "Row",
    (),
    {
        "id": "bs_test123",
        "user_id": "usr_foo",
        "vm_id": "vm_bar",
        "name": "Test Session",
        "kind": "playwright",
        "status": "stopped",
        "browser_path": None,
        "clsid": None,
        "pid": None,
        "host": None,
        "ws_endpoint": None,
        "headless": False,
        "viewport_width": 1280,
        "viewport_height": 800,
        "allowed_hosts": ["localhost", "127.0.0.1"],
        "config": {"foo": "bar"},
        "started_at": None,
        "last_activity_at": None,
        "created_at": datetime(2025, 1, 1),
    },
)


class TestHostAllowed:
    def test_allowed_exact(self):
        assert host_allowed("http://localhost:8000", ["localhost"])
        assert host_allowed("https://127.0.0.1/", ["127.0.0.1"])

    def test_allowed_subdomain(self):
        assert host_allowed("http://app.localhost:8000", ["localhost"])

    def test_blocked(self):
        assert not host_allowed("http://example.com/", ["localhost"])

    def test_about_data_scheme(self):
        assert host_allowed("about:blank", ["x"])
        assert host_allowed("data:text/html,<p>hi", ["x"])

    def test_no_hostname(self):
        # a URL with no hostname matches nothing and must be blocked
        assert not host_allowed("http:///path", ["localhost"])

    def test_mixed_case(self):
        assert host_allowed("HTTP://LOCALHOST/", ["localhost"])


class TestSessionSchema:
    def test_minimal_create(self):
        c = BrowserSessionCreate()
        assert c.kind == "playwright"
        assert c.headless is False

    def test_custom_values(self):
        c = BrowserSessionCreate(
            name="My session",
            kind="dcom_ie",
            url="http://localhost/test",
            headless=False,
            width=1920,
            height=1080,
            allowed_hosts=["10.0.0.1"],
            config={"foo": 1},
        )
        assert c.name == "My session"
        assert c.width == 1920
        assert c.height == 1080
        assert c.allowed_hosts == ["10.0.0.1"]
        assert c.config == {"foo": 1}

    def test_name_truncated(self):
        # the schema caps names at 120 chars (create_session truncates defensively too)
        with pytest.raises(ValidationError):
            BrowserSessionCreate(name="x" * 200)
        c = BrowserSessionCreate(name="x" * 120)
        assert len(c.name) == 120

    def test_unknown_kind(self):
        with pytest.raises(ValidationError):
            BrowserSessionCreate(kind="unknown_kind")

    def test_bad_action(self):
        with pytest.raises(ValidationError):
            BrowserAction(action="warp_speed", x=1)

    def test_valid_action(self):
        a = BrowserAction(action="click", x=100, y=200)
        assert a.signature() == "click:100:200:None:None:0:0:None"

    def test_assertion_spec(self):
        s = AssertionSpec(type="text_visible", value="Hello")
        assert s.name == "text_visible(Hello)"


class TestScreenshotModel:
    def test_from_png(self):
        b = BrowserScreenshot(kind="desktop", width=1280, height=800, b64_png="dGVzdA==")
        assert b.kind == "desktop"
        assert b.width == 1280
        assert b.height == 800
        assert b.timestamp is not None  # defaults to now

    def test_com_kind_accepted(self):
        b = BrowserScreenshot(kind="com", width=800, height=600, b64_png="dGVzdA==")
        assert b.kind == "com"


class TestSessionManager:
    """Tests the manager's persistence layer against a fake row, and the
    permission/allow-list gates without touching the database."""

    @pytest.fixture
    def fake_row(self):
        return FAKE_SQLALCHEMY_ROW()

    def test_from_row(self):
        info = BrowserSessionInfo.from_row(FAKE_SQLALCHEMY_ROW())
        assert info.id == "bs_test123"
        assert info.user_id == "usr_foo"
        assert info.vm_id == "vm_bar"
        assert info.kind == "playwright"
        assert info.viewport_width == 1280
        assert info.allowed_hosts == ["localhost", "127.0.0.1"]

    def test_host_allow_on_navigate(self, fake_row):
        allowed = fake_row.allowed_hosts
        # the session allow-list gates navigation through the driver entry points
        from backend.browser_control.driver import host_allowed
        assert host_allowed("http://localhost/", allowed)
        assert not host_allowed("http://evil.com/", allowed)


@pytest.fixture
def session_manager(tmp_path):
    """Build a manager with a real in-memory SQLite so create_session
    actually hits the DB."""
    class FakeSettings:
        database_url = "sqlite+aiosqlite:///" + str(tmp_path / "test.db")
        data_dir = tmp_path
        browser_control_enabled = True
        browser_dcom_enabled = True
        browser_desktop_capture_enabled = True
        browser_default_timeout_s = 1.0
        browser_allowed_hosts: ClassVar[list[str]] = ["localhost", "127.0.0.1"]
        browser_ie_clsid = "Internet Explorer.Application"
        browser_chrome_clsid = "Chrome HTML Document"
        browser_chrome_path = None
        browser_max_concurrent_sessions = 3
        browser_screenshot_quality = 85
        browser_retry_interval_s = 0.1

    from backend.browser_control.session import SessionManager as SM
    from backend.db.base import Base

    async def go():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        engine = create_async_engine(FakeSettings.database_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = SM(FakeSettings, async_sessionmaker(engine, expire_on_commit=False))
        return sm

    return asyncio.run(go())


def test_session_create_and_crud(tmp_path):
    """Create a session through the manager and verify CRUD round-trip."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.browser_control.models import BrowserSessionCreate
    from backend.browser_control.session import SessionManager as SM
    from backend.db.base import Base

    class FakeSettings:
        database_url = "sqlite+aiosqlite:///" + str(tmp_path / "test2.db")
        data_dir = tmp_path
        browser_control_enabled = True
        browser_dcom_enabled = True
        browser_desktop_capture_enabled = True
        browser_default_timeout_s = 1.0
        browser_allowed_hosts: ClassVar[list[str]] = ["localhost", "127.0.0.1"]
        browser_ie_clsid = "Internet Explorer.Application"
        browser_chrome_clsid = "Chrome HTML Document"
        browser_chrome_path = None
        browser_max_concurrent_sessions = 3

    async def go():
        engine = create_async_engine(FakeSettings.database_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = SM(FakeSettings, async_sessionmaker(engine, expire_on_commit=False))
        body = BrowserSessionCreate(name="Crash test", kind="playwright", headless=True)
        created = await sm.create_session("usr_crash", body)
        assert created.name == "Crash test"
        assert created.kind == "playwright"
        rows = await sm.list_sessions("usr_crash")
        assert len(rows) == 1
        assert rows[0]["id"] == created.id
        # update
        updated = await sm.update_session("usr_crash", created.id, type("U", (), {"name": "renamed", "headless": None,
                                                                                "width": None, "height": None,
                                                                                "config": None})())
        assert updated.name == "renamed"
        # delete round-trip
        await sm.delete_session("usr_crash", created.id)
        rows = await sm.list_sessions("usr_crash")
        assert len(rows) == 0
        return sm, created.id

    sm, sid = asyncio.run(go())
    assert sm.get_sessions is not None
    assert sid
