"""Runs: the `app.js` contract (`POST /api/runs`, `GET /api/runs/{id}`), history, tree, control, streaming."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser, ws_user
from backend.app_context import AppContext
from backend.core.errors import AppError, NotFound
from backend.core.events import TERMINAL_STATUSES, Event, replay_then_follow
from backend.core.ids import iso, utcnow
from backend.db.models import Run

router = APIRouter(tags=["runs"])


class RunCreate(BaseModel):
    text: str = Field(default="", max_length=2_000_000)
    mode: str = "build"
    model: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=50)
    agent_id: str | None = None
    conversation_id: str | None = None
    workspace_id: str | None = None


class EscalationAnswer(BaseModel):
    choice: str = Field(min_length=1, max_length=60)
    message: str | None = Field(default=None, max_length=10_000)
    escalation_id: str | None = None


@router.post("/api/runs", status_code=201)
async def create_run(body: RunCreate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    run = await ctx.runs.create_from_request(user.id, body.model_dump())
    return ctx.runs.serialize(run, [], [], [], None)


@router.get("/api/runs")
async def list_runs(user: CurrentUser, ctx: Ctx, limit: int = Query(20, ge=1, le=100), cursor: str | None = None,
                    mode: str | None = None, status: str | None = None, q: str | None = None,
                    agent_id: str | None = None) -> dict[str, Any]:
    return await ctx.runs.list(user.id, limit=limit, cursor=cursor, mode=mode, status=status, q=q,
                               agent_id=agent_id)


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.get_public(user.id, run_id)


@router.get("/api/runs/{run_id}/tree")
async def run_tree(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.tree(user.id, run_id)


@router.get("/api/runs/{run_id}/diff")
async def run_diff(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.diff(user.id, run_id)


@router.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.runs.delete(user.id, run_id)
    return {"ok": True, "id": run_id}


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.cancel(user.id, run_id)


@router.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.resume(user.id, run_id)


@router.post("/api/runs/{run_id}/escalation")
async def answer_escalation(run_id: str, body: EscalationAnswer, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.runs.answer_escalation(user.id, run_id, choice=body.choice, message=body.message,
                                            escalation_id=body.escalation_id)


# --------------------------------------------------------------------------- streaming
async def _owned_run(ctx: AppContext, user_id: str, run_id: str) -> Run:
    async with ctx.sessions() as session:
        run = await session.get(Run, run_id)
    if run is None or run.user_id != user_id:
        raise NotFound("run not found")
    return run


def _snapshot(run: Run, seq: int) -> dict[str, Any]:
    return Event(type="run.status", seq=seq, ts=iso(utcnow()) or "", run_id=run.id,
                 data={"status": run.status, "mode": run.mode, "final_output": run.final_output,
                       "error": run.error, "snapshot": True}).to_dict()


def _parse_last_seq(value: str | None) -> int:
    try:
        return max(0, int(value)) if value else 0
    except ValueError:
        return 0


@router.get("/api/runs/{run_id}/events")
async def run_events_sse(run_id: str, request: Request, user: CurrentUser, ctx: Ctx,
                         last_seq: int = Query(0, ge=0), follow: bool = True,
                         last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    run = await _owned_run(ctx, user.id, run_id)
    after = max(last_seq, _parse_last_seq(last_event_id))
    sub = ctx.events.subscribe(run_id=run_id)
    backlog = ctx.events.history(run_id, after)

    async def stream() -> AsyncIterator[str]:
        try:
            if not backlog and (run.status in TERMINAL_STATUSES or not follow):
                snap = _snapshot(run, ctx.events.last_seq)
                yield f"id: {snap['seq']}\nevent: run.status\ndata: {json.dumps(snap)}\n\n"
                if run.status in TERMINAL_STATUSES or not follow:
                    return
            if not follow:
                for event in backlog:
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(event.to_dict())}\n\n"
                return
            async for event in replay_then_follow(ctx.events, sub, backlog):
                if await request.is_disconnected():
                    return
                yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(event.to_dict())}\n\n"
                if event.type == "run.status" and event.data.get("status") in TERMINAL_STATUSES:
                    return
        finally:
            sub.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _pump(websocket: WebSocket, events: AsyncIterator[Event], ctx: AppContext) -> None:
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(15)
            await websocket.send_json({"type": "heartbeat", "seq": ctx.events.last_seq, "ts": iso(utcnow())})

    async def writer() -> None:
        async for event in events:
            await websocket.send_json(event.to_dict())

    async def reader() -> None:
        while True:
            message = await websocket.receive_text()
            with contextlib.suppress(ValueError):
                if json.loads(message).get("type") == "ping":
                    await websocket.send_json({"type": "pong", "seq": ctx.events.last_seq})

    tasks = [asyncio.create_task(writer()), asyncio.create_task(reader()), asyncio.create_task(heartbeat())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() and not isinstance(task.exception(), WebSocketDisconnect):
                raise task.exception()  # type: ignore[misc]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/api/runs/{run_id}/stream")
async def run_stream(websocket: WebSocket, run_id: str) -> None:
    ctx: AppContext = websocket.app.state.ctx
    try:
        user = await ws_user(websocket)
        run = await _owned_run(ctx, user.id, run_id)
    except AppError as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 4404, reason=exc.message[:120])
        return
    await websocket.accept()
    after = _parse_last_seq(websocket.query_params.get("last_seq"))
    sub = ctx.events.subscribe(run_id=run_id)
    backlog = ctx.events.history(run_id, after)
    try:
        # A fresh connection gets a status snapshot; a reconnect (last_seq) only gets newer events,
        # unless the run already finished and nothing newer is buffered.
        if not backlog and (after == 0 or run.status in TERMINAL_STATUSES):
            await websocket.send_json(_snapshot(run, ctx.events.last_seq))
        await _pump(websocket, replay_then_follow(ctx.events, sub, backlog), ctx)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sub.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


@router.websocket("/api/events")
async def global_events(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx
    try:
        user = await ws_user(websocket)
    except AppError as exc:
        await websocket.close(code=4401, reason=exc.message[:120])
        return
    await websocket.accept()
    after = _parse_last_seq(websocket.query_params.get("last_seq"))
    sub = ctx.events.subscribe(user_id=user.id)
    backlog = ctx.events.global_history(user.id, after) if after else []
    try:
        await websocket.send_json({"type": "hello", "seq": ctx.events.last_seq, "ts": iso(utcnow()),
                                   "data": {"user_id": user.id}})
        await _pump(websocket, replay_then_follow(ctx.events, sub, backlog), ctx)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sub.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
