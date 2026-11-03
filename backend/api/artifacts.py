"""Run artifact serving: screenshots and logs captured by agents (screen tests, builds).

Paths are the relative paths recorded on artifact rows (e.g. ``artifacts/<run>/xx.png``),
resolved strictly inside the data dir's ``artifacts/`` subtree — no traversal.

``<img>`` tags cannot send an Authorization header, so a ``?token=`` query parameter is
accepted as well — the same pattern the WebSocket stream endpoints already use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from backend.api.deps import Ctx, _bearer, resolve_user
from backend.core.errors import AppError, NotFound

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".txt": "text/plain", ".json": "application/json",
    ".log": "text/plain", ".md": "text/markdown",
}


def _resolve(ctx: Any, rel: str) -> Path:
    root = (ctx.settings.data_dir / "artifacts").resolve()
    if not rel or rel.strip() in ("", ".", "/"):
        raise AppError("artifact path is required", code="invalid_path", status_code=422)
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AppError("artifact path must be relative to the artifacts folder", code="invalid_path",
                       status_code=422)
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise AppError("artifact path escapes the artifacts folder", code="invalid_path", status_code=422)
    if not resolved.is_file():
        raise NotFound("artifact not found")
    return resolved


@router.get("", response_model=None)
async def get_artifact(request: Request, ctx: Ctx, path: str = Query(..., max_length=1000),
                       token: str | None = Query(None)) -> Any:
    await resolve_user(ctx, token or _bearer(request.headers.get("authorization")))
    target = _resolve(ctx, path)
    media = MIME.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media, filename=target.name)
