"""MCP client manager: stdio, streamable-HTTP and SSE servers per user; health, tools, calls.

Each connected server is owned by one long-lived task (anyio requires a context manager to be
entered and exited in the same task); requests are passed to it through a queue.
MCP tools are exposed to agents as ``mcp__<server>__<tool>`` (permission ``mcp.call``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from backend.agents.tools.base import Tool, ToolContext, ToolResult
from backend.core.errors import AppError, Conflict, NotFound
from backend.core.ids import iso
from backend.core.logging import get_logger
from backend.db.models import MCPServer

if TYPE_CHECKING:
    from backend.app_context import AppContext

log = get_logger(__name__)
TRANSPORTS = ("stdio", "http", "sse")
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(name: str) -> str:
    return _SAFE.sub("_", name).strip("_")[:40] or "server"


class MCPConnection:
    def __init__(self, row: MCPServer, env: dict[str, str], headers: dict[str, str], timeout: float = 30) -> None:
        self.server_id = row.id
        self.name = row.name
        self.transport = row.transport
        self.command = row.command
        self.args = list(row.args or [])
        self.url = row.url
        self.env = env
        self.headers = headers
        self.timeout = timeout
        self.tools: list[dict[str, Any]] = []
        self.error: str | None = None
        self.status = "connecting"
        self.connected_at: float | None = None
        self._queue: asyncio.Queue[tuple[Callable[[Any], Awaitable[Any]], asyncio.Future[Any]] | None] = \
            asyncio.Queue()
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _transport_cm(self) -> Any:
        if self.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            return stdio_client(StdioServerParameters(command=self.command or "", args=self.args,
                                                      env=self.env or None))
        if self.transport == "sse":
            from mcp.client.sse import sse_client

            return sse_client(self.url or "", headers=self.headers or None, timeout=self.timeout)
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        client = httpx2.AsyncClient(headers=self.headers or None, timeout=self.timeout)
        return streamable_http_client(self.url or "", http_client=client)

    async def _run(self) -> None:
        from mcp.client.session import ClientSession

        try:
            async with self._transport_cm() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=self.timeout) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = [{"name": t.name, "description": t.description or "",
                                   "input_schema": t.input_schema or {"type": "object", "properties": {}}}
                                  for t in listed.tools]
                    self.status, self.error, self.connected_at = "connected", None, time.time()
                    self._ready.set()
                    while True:
                        item = await self._queue.get()
                        if item is None:
                            break
                        fn, future = item
                        try:
                            result = await asyncio.wait_for(fn(session), timeout=self.timeout)
                            if not future.done():
                                future.set_result(result)
                        except Exception as exc:
                            if not future.done():
                                future.set_exception(exc)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("MCP server %s failed: %s", self.name, self.error)
        finally:
            self.status = "error" if self.error else "disconnected"
            self._ready.set()
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None and not item[1].done():
                    item[1].set_exception(AppError(f"MCP server {self.name} is not connected"))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout + 5)
        except TimeoutError:
            self.error = "timed out while connecting"
            self.status = "error"

    async def request(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        if self.status != "connected":
            raise AppError(f"MCP server '{self.name}' is {self.status}: {self.error or ''}", code="mcp_unavailable",
                           status_code=503)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((fn, future))
        return await future

    async def close(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (TimeoutError, Exception):
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        self.status = "disconnected"


def render_result(result: Any) -> tuple[bool, str]:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(item.text)
        elif kind == "image":
            parts.append(f"[image {getattr(item, 'mime_type', '')}]")
        else:
            parts.append(json.dumps(item.model_dump(mode="json"), default=str)[:2000])
    structured = getattr(result, "structured_content", None)
    if structured and not parts:
        parts.append(json.dumps(structured, default=str))
    return not bool(getattr(result, "is_error", False)), "\n".join(parts)


class MCPManager:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self._connections: dict[str, MCPConnection] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ persistence
    def _decrypt_json(self, token: str | None) -> dict[str, str]:
        if not token:
            return {}
        try:
            return dict(json.loads(self.app.secrets.decrypt(token)))
        except Exception:
            return {}

    def public(self, row: MCPServer) -> dict[str, Any]:
        conn = self._connections.get(row.id)
        return {"id": row.id, "name": row.name, "transport": row.transport, "command": row.command,
                "args": row.args, "url": row.url, "enabled": row.enabled,
                "has_env": bool(row.encrypted_env), "has_headers": bool(row.encrypted_headers),
                "status": conn.status if conn else ("disabled" if not row.enabled else row.last_status),
                "error": conn.error if conn else row.last_error,
                "tools": [t["name"] for t in conn.tools] if conn else [], "created_at": iso(row.created_at)}

    async def _row(self, user_id: str, server_id: str) -> MCPServer:
        async with self.app.sessions() as session:
            row = await session.get(MCPServer, server_id)
        if row is None or row.user_id != user_id:
            raise NotFound("MCP server not found")
        return row

    async def list_servers(self, user_id: str) -> list[dict[str, Any]]:
        async with self.app.sessions() as session:
            rows = list(await session.scalars(select(MCPServer).where(MCPServer.user_id == user_id)
                                              .order_by(MCPServer.created_at)))
        return [self.public(r) for r in rows]

    async def create(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        transport = payload.get("transport", "stdio")
        if transport not in TRANSPORTS:
            raise AppError(f"transport must be one of {TRANSPORTS}", code="invalid_transport", status_code=422)
        if transport == "stdio" and not payload.get("command"):
            raise AppError("stdio servers need a command", code="invalid_mcp", status_code=422)
        if transport != "stdio" and not str(payload.get("url", "")).startswith(("http://", "https://")):
            raise AppError("http/sse servers need an http(s) url", code="invalid_mcp", status_code=422)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise AppError("name is required", code="invalid_mcp", status_code=422)
        async with self.app.sessions() as session:
            if await session.scalar(select(MCPServer).where(MCPServer.user_id == user_id, MCPServer.name == name)):
                raise Conflict(f"an MCP server named '{name}' already exists", code="duplicate_mcp")
            row = MCPServer(user_id=user_id, name=name, transport=transport, command=payload.get("command"),
                            args=list(payload.get("args") or []), url=payload.get("url"),
                            enabled=bool(payload.get("enabled", True)),
                            encrypted_env=self.app.secrets.encrypt(json.dumps(payload["env"]))
                            if payload.get("env") else None,
                            encrypted_headers=self.app.secrets.encrypt(json.dumps(payload["headers"]))
                            if payload.get("headers") else None)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        if row.enabled:
            await self.connect(row)
        return self.public(row)

    async def set_enabled(self, user_id: str, server_id: str, enabled: bool) -> dict[str, Any]:
        row = await self._row(user_id, server_id)
        async with self.app.sessions() as session:
            db_row = await session.get(MCPServer, server_id)
            assert db_row is not None
            db_row.enabled = enabled
            await session.commit()
            await session.refresh(db_row)
            row = db_row
        if enabled:
            await self.connect(row)
        else:
            await self.disconnect(server_id)
        return self.public(row)

    async def delete(self, user_id: str, server_id: str) -> None:
        await self._row(user_id, server_id)
        await self.disconnect(server_id)
        async with self.app.sessions() as session:
            row = await session.get(MCPServer, server_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    # ------------------------------------------------------------------ connections
    async def connect(self, row: MCPServer) -> MCPConnection:
        async with self._lock:
            await self.disconnect(row.id, lock=False)
            conn = MCPConnection(row, self._decrypt_json(row.encrypted_env), self._decrypt_json(row.encrypted_headers))
            self._connections[row.id] = conn
            await conn.start()
        async with self.app.sessions() as session:
            db_row = await session.get(MCPServer, row.id)
            if db_row is not None:
                db_row.last_status = conn.status
                db_row.last_error = conn.error
                await session.commit()
        return conn

    async def disconnect(self, server_id: str, lock: bool = True) -> None:
        conn = self._connections.pop(server_id, None)
        if conn is not None:
            await conn.close()

    async def connection(self, user_id: str, server_id: str) -> MCPConnection:
        row = await self._row(user_id, server_id)
        conn = self._connections.get(server_id)
        if conn is None or conn.status != "connected":
            if not row.enabled:
                raise AppError("MCP server is disabled", code="mcp_disabled", status_code=409)
            conn = await self.connect(row)
        return conn

    async def health(self, user_id: str, server_id: str) -> dict[str, Any]:
        row = await self._row(user_id, server_id)
        conn = self._connections.get(server_id)
        if conn is None or conn.status != "connected":
            return {"id": server_id, "ok": False, "status": conn.status if conn else "disconnected",
                    "error": conn.error if conn else row.last_error}
        started = time.perf_counter()
        try:
            await conn.request(lambda s: s.send_ping())
            return {"id": server_id, "ok": True, "status": "connected",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1), "tools": len(conn.tools)}
        except Exception as exc:
            return {"id": server_id, "ok": False, "status": "error", "error": str(exc)[:300]}

    async def tools(self, user_id: str, server_id: str) -> list[dict[str, Any]]:
        conn = await self.connection(user_id, server_id)
        return conn.tools

    async def call(self, user_id: str, server_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        conn = await self.connection(user_id, server_id)
        if tool not in {t["name"] for t in conn.tools}:
            raise NotFound(f"tool '{tool}' not found on {conn.name}")
        result = await conn.request(lambda s: s.call_tool(tool, arguments))
        ok, text = render_result(result)
        return {"ok": ok, "content": text, "structured": getattr(result, "structured_content", None)}

    async def start_enabled(self) -> None:
        async with self.app.sessions() as session:
            rows = list(await session.scalars(select(MCPServer).where(MCPServer.enabled)))
        for row in rows:
            try:
                await self.connect(row)
            except Exception:
                log.exception("could not connect MCP server %s", row.name)

    async def shutdown(self) -> None:
        for server_id in list(self._connections):
            await self.disconnect(server_id)

    async def status(self, user_id: str) -> dict[str, Any]:
        servers = await self.list_servers(user_id)
        return {"ok": all(s["status"] in ("connected", "disabled") for s in servers), "servers": servers}

    # ------------------------------------------------------------------ agent tools
    async def agent_tools(self, ctx: ToolContext) -> list[Tool]:
        async with self.app.sessions() as session:
            rows = list(await session.scalars(select(MCPServer).where(MCPServer.user_id == ctx.user_id,
                                                                      MCPServer.enabled)))
        tools: list[Tool] = []
        for row in rows:
            conn = self._connections.get(row.id)
            if conn is None or conn.status != "connected":
                continue
            for spec in conn.tools:
                tools.append(MCPTool(self, row.id, row.name, spec))
        return tools


class MCPTool(Tool):
    permission = "mcp.call"

    def __init__(self, manager: MCPManager, server_id: str, server_name: str, spec: dict[str, Any]) -> None:
        self.manager = manager
        self.server_id = server_id
        self.remote_name = spec["name"]
        self.name = f"mcp__{_slug(server_name)}__{_slug(spec['name'])}"[:64]
        self.description = f"[MCP {server_name}] {spec.get('description', '')}"[:1000]
        self.parameters = spec.get("input_schema") or {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        result = await self.manager.call(ctx.user_id, self.server_id, self.remote_name, args)
        return ToolResult(ok=bool(result["ok"]), content=result["content"][:20_000] or "(empty result)")
