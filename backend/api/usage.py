"""Agent-facing tools catalog.

Usage analytics live in ``backend/api/system.py`` (``/api/usage`` with
``group_by=model|provider|category|day``) — this module only adds the tool
catalog the permissions editor consumes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.api.deps import Ctx, CurrentUser

router = APIRouter(tags=["usage"])


@router.get("/api/tools")
async def tools_catalog(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    """Every registered tool with its permission, grouped client-side by prefix."""
    return [{"name": t.name, "permission": t.permission, "description": t.description}
            for t in sorted(ctx.tools._tools.values(), key=lambda t: (t.permission, t.name))]
