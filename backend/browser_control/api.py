"""Browser-control REST API: create/start/stop sessions, perform actions, take screenshots.

Prefix: /api/browser-control/
Ties into the existing VM infrastructure: a browser-control session can be bound to a
sandbox VM (vm_id) so the agent drives a browser inside that environment.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from backend.api.deps import Ctx, CurrentUser
from backend.browser_control import SessionManager
from backend.browser_control.models import (
    AssertionSpec,
    BrowserAction,
    BrowserActionResult,
    BrowserScreenshot,
    BrowserSessionCreate,
    BrowserSessionInfo,
    BrowserSessionUpdate,
)
from backend.core.errors import NotFound

router = APIRouter(prefix="/api/browser-control", tags=["browser-control"])

browser_sessions = SessionManager  # singleton bound lazily by FastAPI


class StartBody(BaseModel):
    url: str | None = None
    vm_id: str | None = None


class ActionBody(BaseModel):
    action: BrowserAction


@router.get("/sessions", response_model=list[BrowserSessionInfo])
async def list_sessions(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.list_sessions(user.id)


@router.post("/sessions", response_model=BrowserSessionInfo, status_code=201)
async def create_session(body: BrowserSessionCreate, user: CurrentUser, ctx: Ctx) -> BrowserSessionInfo:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.create_session(user.id, body)


@router.get("/sessions/{session_id}", response_model=BrowserSessionInfo)
async def get_session(session_id: str, user: CurrentUser, ctx: Ctx) -> BrowserSessionInfo:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    info = await manager.get_session_by_id(user.id, session_id)
    if info is None:
        raise NotFound("browser session not found")
    return info


@router.patch("/sessions/{session_id}", response_model=BrowserSessionInfo)
async def update_session(session_id: str, body: BrowserSessionUpdate, user: CurrentUser,
                         ctx: Ctx) -> BrowserSessionInfo:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.update_session(user.id, session_id, body)


@router.delete("/sessions/{session_id}", response_model=dict[str, Any])
async def delete_session(session_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    await manager.delete_session(user.id, session_id)
    return {"ok": True, "id": session_id}


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: str, body: StartBody, user: CurrentUser, ctx: Ctx) -> BrowserSessionInfo:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.start_session(user.id, session_id, url=body.url, vm_id=body.vm_id)


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, user: CurrentUser, ctx: Ctx) -> BrowserSessionInfo:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.stop_session(user.id, session_id)


@router.post("/sessions/{session_id}/action")
async def perform_action(session_id: str, body: ActionBody, user: CurrentUser, ctx: Ctx) -> BrowserActionResult:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.perform_action(user.id, session_id, body.action)


@router.get("/sessions/{session_id}/screenshot")
async def screenshot(session_id: str, user: CurrentUser, ctx: Ctx) -> BrowserScreenshot:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.take_screenshot(user.id, session_id)


@router.post("/sessions/{session_id}/assert")
async def check_assertion(session_id: str, spec: AssertionSpec, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    return await manager.check_assertion(user.id, session_id, spec)


@router.get("/active")
async def list_active_sessions(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    manager: SessionManager = ctx.extras.get("browser_sessions")
    rows = await manager.list_sessions(user.id)
    return [r for r in rows if r.get("status") == "running"]
