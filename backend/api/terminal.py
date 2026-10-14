"""Interactive terminal: an xterm-compatible PTY session inside the sandbox over WebSocket.

Client → server: ``{"type":"stdin","data":"..."}``, ``{"type":"resize","cols":120,"rows":32}``, ``{"type":"ping"}``
Server → client: ``{"type":"ready",...}``, ``{"type":"stdout","data":"..."}``, ``{"type":"exit","code":0}``,
``{"type":"error","message":"..."}``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.api.deps import ws_user
from backend.app_context import AppContext
from backend.core.errors import AppError
from backend.core.logging import get_logger
from backend.sandbox.pty import PtySession, spawn_pty

log = get_logger(__name__)
router = APIRouter(tags=["terminal"])


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value))) if value else default
    except ValueError:
        return default


@router.websocket("/api/terminal")
async def terminal(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx
    try:
        user = await ws_user(websocket)
    except AppError as exc:
        await websocket.close(code=4401, reason=exc.message[:120])
        return
    await websocket.accept()
    cols = _int(websocket.query_params.get("cols"), 120, 20, 500)
    rows = _int(websocket.query_params.get("rows"), 32, 5, 200)
    try:
        sandbox = await ctx.sandboxes.for_terminal(user.id, ctx.settings.workspace_root,
                                                   websocket.query_params.get("vm_id") or None)
        argv, cwd, env = sandbox.pty_argv()
        pty: PtySession = await asyncio.to_thread(spawn_pty, argv, cwd, env, cols, rows)
    except (AppError, OSError, RuntimeError) as exc:
        message = exc.message if isinstance(exc, AppError) else f"{type(exc).__name__}: {exc}"
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=4503)
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            try:
                data = pty.read()
            except Exception:
                data = ""
            if not data:
                if not pty.alive() or stop.is_set():
                    break
                continue
            loop.call_soon_threadsafe(queue.put_nowait, data)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    reader = threading.Thread(target=pump, name="jevora-pty-reader", daemon=True)
    reader.start()
    await websocket.send_json({"type": "ready", "sandbox": sandbox.describe(), "cols": cols, "rows": rows})

    async def outbound() -> None:
        while True:
            data = await queue.get()
            if data is None:
                await websocket.send_json({"type": "exit", "code": pty.exit_code})
                return
            chunks = [data]
            while not queue.empty() and sum(map(len, chunks)) < 65536:
                more = queue.get_nowait()
                if more is None:
                    queue.put_nowait(None)
                    break
                chunks.append(more)
            await websocket.send_json({"type": "stdout", "data": "".join(chunks)})

    async def inbound() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                message = {"type": "stdin", "data": raw}
            kind = message.get("type")
            if kind == "stdin":
                await asyncio.to_thread(pty.write, str(message.get("data", "")))
            elif kind == "resize":
                pty.resize(_int(str(message.get("cols")), cols, 20, 500), _int(str(message.get("rows")), rows, 5, 200))
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})

    tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(pty.close)
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
