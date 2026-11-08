"""Installed skills (SKILL.md packages) the agent can load with use_skill."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.agents.skills import discover_skills
from backend.api.deps import Ctx, CurrentUser

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
async def list_skills(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    global_dir = ctx.settings.data_dir / "skills"
    skills = discover_skills(ctx.settings.workspace_root, global_dir)
    return {"global_dir": str(global_dir),
            "project_dir": str(ctx.settings.workspace_root / ".jevora" / "skills"),
            "skills": [{"name": s.name, "description": s.description, "source": s.source, "path": str(s.path)}
                       for s in skills]}
