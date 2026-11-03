"""Projects: workspace CRUD for the UI's Projects sidebar.

Two ways to make a project:
  * ``POST /api/projects``  — app-controlled: a name-sanitised subdirectory under
    the workspace root (never an arbitrary host path).
  * ``POST /api/projects/open`` — work in an existing folder on this machine
    (desktop app folder picker). The folder must exist; it is registered as a
    workspace so runs, file tools and the terminal operate inside it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import AppError, Conflict, NotFound
from backend.db.models import Conversation, Workspace

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectOpen(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@router.get("")
async def list_projects(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.sessions() as session:
        rows = list(await session.scalars(select(Workspace).where(
            Workspace.user_id == user.id).order_by(Workspace.created_at)))
    return [{"id": w.id, "name": w.name, "root_path": w.root_path, "is_default": w.is_default,
             "created_at": w.created_at.isoformat()} for w in rows]


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


@router.post("/open", status_code=201)
async def open_project(body: ProjectOpen, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    """Register an existing folder on this machine as a project (workspace)."""
    raw = body.path.strip().strip('"')
    if not raw:
        raise AppError("folder path is required", code="invalid_path", status_code=422)
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise AppError("folder path must be absolute", code="invalid_path", status_code=422)
    if not folder.is_dir():
        raise AppError(f"'{raw}' is not a folder on this machine", code="folder_not_found", status_code=404)
    resolved = str(folder.resolve())
    name = folder.name or "Project"
    async with ctx.sessions() as session:
        existing = await session.scalar(select(Workspace).where(
            Workspace.user_id == user.id, Workspace.root_path == resolved))
        if existing is not None:
            return {"id": existing.id, "name": existing.name, "root_path": existing.root_path,
                    "is_default": existing.is_default, "created_at": existing.created_at.isoformat(),
                    "existing": True}
        # keep per-user names unique without failing the open
        base, candidate, suffix = name, name, 2
        while await session.scalar(select(Workspace).where(
                Workspace.user_id == user.id, Workspace.name == candidate)) is not None:
            candidate = f"{base} ({suffix})"
            suffix += 1
        workspace = Workspace(user_id=user.id, name=candidate[:200], root_path=resolved)
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)
    return {"id": workspace.id, "name": workspace.name, "root_path": workspace.root_path,
            "is_default": workspace.is_default, "created_at": workspace.created_at.isoformat()}


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        workspace = await session.get(Workspace, project_id)
        if workspace is None or workspace.user_id != user.id:
            raise NotFound("project not found")
        # Chats survive: they simply become standalone conversations again.
        await session.execute(update(Conversation)
                              .where(Conversation.project_id == project_id).values(project_id=None))
        await session.delete(workspace)
        await session.commit()
    return {"ok": True, "id": project_id}
