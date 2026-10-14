"""Workspace browsing for the File Preview drawer: tree, read, write, delete, change feed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser
from backend.app_context import AppContext
from backend.core.errors import AppError, NotFound
from backend.core.paths import IGNORED_DIRS, WorkspaceGuard, detect_language, is_binary

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 5 * 1024 * 1024


def guard_for(ctx: AppContext) -> WorkspaceGuard:
    return WorkspaceGuard(ctx.settings.workspace_root)


def _node(guard: WorkspaceGuard, path: Path, depth: int, budget: list[int]) -> dict[str, Any]:
    rel = guard.relative(path)
    if path.is_dir():
        node: dict[str, Any] = {"name": path.name or rel, "path": rel, "type": "dir", "children": []}
        if depth <= 0:
            node["truncated"] = True
            return node
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return node
        for entry in entries:
            if budget[0] <= 0:
                node["truncated"] = True
                break
            if entry.is_dir(follow_symlinks=False) and entry.name in IGNORED_DIRS:
                continue
            if entry.is_symlink():
                try:
                    guard.resolve(entry.path)
                except AppError:
                    continue  # never expose symlinks that point outside the workspace
            budget[0] -= 1
            node["children"].append(_node(guard, Path(entry.path), depth - 1, budget))
        return node
    stat = path.stat()
    return {"name": path.name, "path": rel, "type": "file", "size": stat.st_size,
            "language": detect_language(rel), "modified": stat.st_mtime}


@router.get("")
async def workspace_info(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    root = ctx.settings.workspace_root
    return {"root": str(root), "name": root.name, "sandbox": await ctx.sandboxes.status()}


@router.get("/tree")
async def tree(user: CurrentUser, ctx: Ctx, path: str = Query("."), depth: int = Query(6, ge=1, le=20),
               limit: int = Query(3000, ge=10, le=20000)) -> dict[str, Any]:
    guard = guard_for(ctx)
    base = guard.resolve(path)
    if not base.exists():
        raise NotFound(f"{path} does not exist")
    return _node(guard, base, depth, [limit])


@router.get("/file", response_model=None)
async def read_file(user: CurrentUser, ctx: Ctx, path: str = Query(...), raw: bool = False) -> Any:
    guard = guard_for(ctx)
    target = guard.resolve(path)
    if not target.is_file():
        raise NotFound(f"{path} is not a file")
    if raw:
        return FileResponse(target, filename=target.name)
    stat = target.stat()
    rel = guard.relative(target)
    with target.open("rb") as handle:
        data = handle.read(MAX_PREVIEW_BYTES + 1)
    truncated = len(data) > MAX_PREVIEW_BYTES
    data = data[:MAX_PREVIEW_BYTES]
    base = {"path": rel, "size": stat.st_size, "language": detect_language(rel), "modified": stat.st_mtime,
            "truncated": truncated}
    if is_binary(data[:8192]):
        return {**base, "binary": True, "content": None, "encoding": None}
    return {**base, "binary": False, "content": data.decode("utf-8", "replace"), "encoding": "utf-8"}


class FileWrite(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=MAX_WRITE_BYTES)


@router.put("/file")
async def write_file(body: FileWrite, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    guard = guard_for(ctx)
    target = guard.resolve(body.path, for_write=True)
    if target.is_dir():
        raise AppError(f"{body.path} is a directory", code="is_directory", status_code=422)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.jevora-tmp")
    tmp.write_text(body.content, encoding="utf-8", newline="")
    os.replace(tmp, target)
    rel = guard.relative(target)
    size = target.stat().st_size
    ctx.events.publish("file.changed", user_id=user.id, data={
        "path": rel, "change": "modified" if existed else "created", "size": size,
        "language": detect_language(rel), "source": "user"})
    return {"path": rel, "size": size, "created": not existed}


@router.delete("/file")
async def delete_file(user: CurrentUser, ctx: Ctx, path: str = Query(...)) -> dict[str, Any]:
    guard = guard_for(ctx)
    target = guard.resolve(path, for_write=True)
    if not target.is_file():
        raise NotFound(f"{path} is not a file")
    target.unlink()
    rel = guard.relative(target)
    ctx.events.publish("file.changed", user_id=user.id, data={"path": rel, "change": "deleted", "source": "user"})
    return {"ok": True, "path": rel}


@router.get("/changes")
async def changes(user: CurrentUser, ctx: Ctx, since_seq: int = Query(0, ge=0),
                  limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    # Agent (run-scoped) and user file events both land in the global buffer.
    events = [e for e in ctx.events.global_history(user.id, since_seq) if e.type == "file.changed"]
    items = [{"seq": e.seq, "ts": e.ts, "run_id": e.run_id, "call_id": e.call_id, **e.data} for e in events[-limit:]]
    return {"items": items, "last_seq": ctx.events.last_seq}
