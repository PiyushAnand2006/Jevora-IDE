"""Standalone tests for browser-control schemas and host allow-lists.

These do not import backend.main, so they do not require langgraph.checkpoint.postgres
or a running database. Run with: pytest tests/standalone_browser_test.py -v
"""
from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from backend.browser_control.driver import host_allowed
from backend.browser_control.models import (
    AssertionSpec,
    BrowserAction,
    BrowserScreenshot,
    BrowserSessionCreate,
    BrowserSessionInfo,
)


class TestHostAllowed:
    def test_allowed_exact(self):
        assert host_allowed("http://localhost:8000", ["localhost"])

    def test_allowed_subdomain(self):
        assert host_allowed("http://app.localhost:8000", ["localhost"])

    def test_allowed_loopback(self):
        assert host_allowed("https://127.0.0.1/", ["127.0.0.1"])

    def test_blocked(self):
        assert not host_allowed("http://example.com/", ["localhost"])

    def test_about_data_scheme(self):
        assert host_allowed("about:blank", ["x"])
        assert host_allowed("data:text/html,<p>hi", ["x"])

    def test_mixed_case(self):
        assert host_allowed("HTTP://LOCALHOST/", ["localhost"])

    def test_empty_allowlist_blocks(self):
        assert not host_allowed("http://evil.com/", [])


class TestSessionSchema:
    def test_minimal_create(self):
        c = BrowserSessionCreate()
        assert c.kind == "playwright"
        assert c.headless is False
        assert c.width == 1280
        assert c.height == 800

    def test_full_create(self):
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
        assert c.kind == "dcom_ie"
        assert c.url == "http://localhost/test"
        assert c.width == 1920
        assert c.height == 1080
        assert c.allowed_hosts == ["10.0.0.1"]
        assert c.config == {"foo": 1}

    def test_name_truncated(self):
        c = BrowserSessionCreate(name="x" * 120)
        assert len(c.name) == 120

    def test_bad_kind(self):
        with pytest.raises(Exception):
            BrowserSessionCreate(kind="unknown_kind")

    def test_valid_action(self):
        a = BrowserAction(action="click", x=100, y=200)
        assert a.action == "click"
        assert a.x == 100
        assert a.y == 200

    def test_done_action(self):
        a = BrowserAction(action="done", reason="goal achieved")
        assert a.action == "done"

    def test_assertion_spec(self):
        s = AssertionSpec(type="text_visible", value="Hello")
        assert s.name == "text_visible(Hello)"
        s2 = AssertionSpec(type="url_contains", value="localhost")
        assert s2.name == "url_contains(localhost)"

    def test_screenshot_model(self):
        b = BrowserScreenshot(kind="desktop", width=1280, height=800, b64_png="dGVzdA==",
                              timestamp=__import__("datetime").datetime(2025, 1, 1))
        assert b.kind == "desktop"
        assert b.width == 1280
        assert b.height == 800


FAKE_ROW = type(
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
        "created_at": __import__("datetime").datetime(2025, 1, 1),
        "timestamp": None,
    },
)


def test_from_row():
    info = BrowserSessionInfo.from_row(FAKE_ROW())
    assert info.id == "bs_test123"
    assert info.user_id == "usr_foo"
    assert info.vm_id == "vm_bar"
    assert info.kind == "playwright"
    assert info.status == "stopped"
    assert info.viewport_width == 1280
    assert info.viewport_height == 800
    assert info.allowed_hosts == ["localhost", "127.0.0.1"]
    assert info.config == {"foo": "bar"}
    assert info.created_at is not None


def test_session_manager_create_and_crud(tmp_path):
    """Create a session through the manager against a real SQLite DB.

    Each run gets its own database file (tmp_path) so rows never leak between runs.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.browser_control.models import BrowserSessionCreate
    from backend.browser_control.session import SessionManager as SM
    from backend.db.base import Base

    db_path = tmp_path / "crud.db"

    class Settings:
        database_url = "sqlite+aiosqlite:///" + str(db_path)
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
        engine = create_async_engine(Settings.database_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = SM(Settings, async_sessionmaker(engine, expire_on_commit=False))
        body = BrowserSessionCreate(name="Crash test", kind="playwright", headless=True)
        created = await sm.create_session("usr_crash", body)
        assert created.name == "Crash test"
        assert created.kind == "playwright"
        rows = await sm.list_sessions("usr_crash")
        assert len(rows) == 1
        assert rows[0]["id"] == created.id
        assert rows[0]["user_id"] == "usr_crash"
        # update
        from pydantic import BaseModel
        class Update(BaseModel):
            name: str | None = None
            headless: bool | None = None
            width: int | None = None
            height: int | None = None
            config: dict | None = None
        updated = await sm.update_session("usr_crash", created.id, Update(name="renamed"))
        assert updated.name == "renamed"
        # user isolation: another user sees nothing
        assert await sm.list_sessions("usr_other") == []
        return sm, created.id

    sm, sid = asyncio.run(go())
    assert sm is not None
    assert sid.startswith("bs_")




if __name__ == "__main__":
    pytest.main([__file__, "-v"])
