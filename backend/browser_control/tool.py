"""Agent tool for browser control.

``browser_control`` - operate on browser-control sessions: create, start, stop, perform
actions, take screenshots, and check assertions. Mirrors the DCOM/remote-control surface
identified in Antigravity.exe while reusing the existing screen-control primitives
(Playwright + desktop capture drivers).
"""
from __future__ import annotations

from typing import Any

from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema
from backend.browser_control import SessionManager
from backend.browser_control.models import (
    AssertionSpec,
    BrowserAction,
    BrowserSessionCreate,
)


class BrowserControlTool(Tool):
    name = "browser_control"
    description = (
        "Operate on browser-control sessions (DCOM/IE, Chrome, Playwright headless, or "
        "desktop capture). Use to drive a browser inside an agent's environment and "
        "observe the result. Permissions are enforced: only sessions owned by the caller "
        "or its VM may be manipulated, and navigation is gated by the session allow-list."
    )
    permission = "browser.control"
    parameters = schema({
        "operation": {"type": "string", "enum": ["create", "start", "stop", "list",
                                                  "action", "screenshot", "assert"]},
        "session_id": {"type": "string"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["dcom_ie", "dcom_chrome", "playwright", "desktop"]},
        "url": {"type": "string"},
        "action": {"type": "object"},
        "spec": {"type": "object"},
    }, ["operation"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        rc = ctx.extras.get("run_context")
        if rc is None:
            return ToolResult(ok=False, content="error: browser control needs a run context")
        manager: SessionManager = ctx.app.extras.get("browser_sessions")
        if manager is None:
            return ToolResult(ok=False, content="error: browser sessions not initialized")

        op = args.get("operation", "").lower()

        if op == "create":
            body = BrowserSessionCreate(name=str(args.get("name", "Browser Control Session")),
                                        kind=args.get("kind", "playwright"),
                                        url=args.get("url"), headless=args.get("headless", False),
                                        width=args.get("width", 1280), height=args.get("height", 800),
                                        allowed_hosts=args.get("allowed_hosts"))
            info = await manager.create_session(rc.user_id, body)
            return ToolResult(ok=True, content=f"created session {info.id} (kind={info.kind}, status={info.status})")

        if op == "list":
            rows = await manager.list_sessions(rc.user_id)
            lines = [f"{r['id']}: {r['kind']} {r['status']} {'(running)' if r['status']=='running' else ''}"
                     for r in rows]
            return ToolResult(ok=True, content="\n".join(lines))

        if op in ("start", "stop"):
            sid = args.get("session_id")
            if not sid:
                return ToolResult(ok=False, content="session_id required")
            if op == "start":
                info = await manager.start_session(rc.user_id, sid, url=args.get("url"), vm_id=args.get("vm_id"))
            else:
                info = await manager.stop_session(rc.user_id, sid)
            return ToolResult(ok=True, content=f"{op} session {sid}: {info.status}")

        if op == "action":
            sid = args.get("session_id")
            if not sid:
                return ToolResult(ok=False, content="session_id required")
            act = BrowserAction(**args.get("action", {}))
            result = await manager.perform_action(rc.user_id, sid, act)
            return ToolResult(ok=result.ok, content=result.message)

        if op == "screenshot":
            sid = args.get("session_id")
            if not sid:
                return ToolResult(ok=False, content="session_id required")
            img = await manager.take_screenshot(rc.user_id, sid)
            # attach the PNG to the tool result so the caller can see it
            return ToolResult(ok=True, content=f"screenshot captured ({img.width}x{img.height})",
                              evidence=[("screenshot", img.b64_png, "image/png")])

        if op == "assert":
            sid = args.get("session_id")
            if not sid:
                return ToolResult(ok=False, content="session_id required")
            spec = args.get("spec") or {}
            assertion = await manager.check_assertion(
                rc.user_id, sid, spec if isinstance(spec, AssertionSpec) else AssertionSpec(**spec))
            return ToolResult(ok=assertion.get("passed", False),
                              content=f"{assertion['name']}: {'PASS' if assertion.get('passed') else 'FAIL'}")

        return ToolResult(ok=False, content=f"unknown operation: {op}")
