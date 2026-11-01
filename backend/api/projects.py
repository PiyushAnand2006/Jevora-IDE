"""Projects: workspace CRUD for the UI's Projects panel.

Workspaces are directories under the configured workspace root — creation is
app-controlled (name-sanitised subdirectory), never an arbitrary host path.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import AppError, Conflict
from backend.db.models import Workspace

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.get("")
async def list_projects(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.sessions() as session:
        rows = list(await session.scalars(select(Workspace).where(
            Workspace.user_id == user.id).order_by(Workspace.created_at)))
    return [{"id": w.id, "name": w.name, "is_default": w.is_default, "created_at": w.created_at.isoformat()}
            for w in rows]


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    name = body.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,79}", name):
        raise AppError("project name may contain letters, digits, spaces, '-' and '_'",
                       code="invalid_name", status_code=422)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    async with ctx.sessions() as session:
        existing = await session.scalar(select(Workspace).where(
            Workspace.user_id == user.id, Workspace.name == name))
        if existing is not None:
            raise Conflict(f"a project named '{name}' already exists")
        root = ctx.settings.workspace_root / "projects" / slug
        root.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(user_id=user.id, name=name, root_path=str(root))
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)
    return {"id": workspace.id, "name": workspace.name, "root_path": workspace.root_path,
            "is_default": workspace.is_default, "created_at": workspace.created_at.isoformat()}
