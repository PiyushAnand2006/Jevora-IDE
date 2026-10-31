"""Pydantic models for browser control: session CRUD, actions, and observations.

These mirror the existing `screen_control` model layer but extend it with DCOM/
browser-control-specific fields (CLSIDs, browser paths, host attachment, session
lifecycle) that were identified in the reverse engineering of Antigravity.exe:
a Delphi application that drives browsers via OLE/IDispatch (DCOM) and records
session metadata for auditability.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class BrowserSessionCreate(BaseModel):
    name: str = Field(default="Browser Control Session", max_length=120)
    kind: Literal["dcom_ie", "dcom_chrome", "playwright", "desktop"] = "playwright"
    url: str | None = None
    headless: bool = False
    width: int = 1280
    height: int = 800
    allowed_hosts: list[str] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class BrowserSessionUpdate(BaseModel):
    name: str | None = None
    headless: bool | None = None
    width: int | None = None
    height: int | None = None
    config: dict[str, Any] | None = None


class BrowserSessionInfo(BaseModel):
    id: str
    user_id: str
    vm_id: str | None
    name: str
    kind: str
    status: str
    browser_path: str | None
    clsid: str | None
    pid: int | None
    host: str | None
    ws_endpoint: str | None
    headless: bool
    viewport_width: int
    viewport_height: int
    allowed_hosts: list[str]
    config: dict[str, Any]
    started_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> BrowserSessionInfo:
        return cls(
            id=row.id,
            user_id=row.user_id,
            vm_id=row.vm_id,
            name=row.name,
            kind=row.kind,
            status=row.status,
            browser_path=row.browser_path,
            clsid=row.clsid,
            pid=row.pid,
            host=row.host,
            ws_endpoint=row.ws_endpoint,
            headless=row.headless,
            viewport_width=row.viewport_width,
            viewport_height=row.viewport_height,
            allowed_hosts=row.allowed_hosts,
            config=row.config,
            started_at=row.started_at,
            last_activity_at=row.last_activity_at,
            created_at=row.created_at,
        )


class BrowserAction(BaseModel):
    """A browser-control action submitted by an agent or the screen-test loop."""
    action: Literal["click", "double_click", "type", "scroll", "key", "wait",
                    "navigate", "done", "screenshot", "dom_query", "js_eval"] = "done"
    x: int | None = None
    y: int | None = None
    text: str | None = None
    dx: int = 0
    dy: int = 0
    key: str | None = None
    ms: int | None = None
    url: str | None = None
    selector: str | None = None        # dom_query / playwright-style selector
    script: str | None = None          # js_eval payload
    reason: str = ""

    def signature(self) -> str:
        return f"{self.action}:{self.x}:{self.y}:{self.text}:{self.key}:{self.dx}:{self.dy}:{self.url}"


class BrowserActionResult(BaseModel):
    ok: bool
    message: str = ""
    url: str | None = None
    screenshot: str | None = None       # b64 PNG, for dom_query/screenshot actions
    data: dict[str, Any] = Field(default_factory=dict)  # js_eval result, dom snapshot


class BrowserScreenshot(BaseModel):
    """Captured screen frame: from DCOM (IE/Chrome) via its window capture or from
    the desktop driver via BitBlt/PrintWindow."""
    kind: Literal["com", "desktop", "playwright"]
    width: int
    height: int
    b64_png: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserControlState(BaseModel):
    """Per-session observation state maintained by the driver: console/network errors,
    action log, current URL and page title."""
    url: str = ""
    title: str = ""
    console_errors: list[str] = Field(default_factory=list)
    network_errors: list[str] = Field(default_factory=list)
    action_log: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_history: list[str] = Field(default_factory=list)  # b64 of recent frames


class AssertionSpec(BaseModel):
    type: Literal["text_visible", "title_contains", "url_contains", "selector_visible",
                  "no_console_errors", "element_count"] = "text_visible"
    value: str = ""
    count: int | None = None

    @property
    def name(self) -> str:
        return f"{self.type}({self.value})" if self.value else self.type
