"""Obsidian vault: config, run notes listing/reading, and reindex (vault → DB)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import NotFound
from backend.orchestrator.records import build_tree

router = APIRouter(prefix="/api/vault", tags=["vault"])


class VaultConfig(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@router.get("/config")
async def get_config(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.vault.status(user.id)


@router.put("/config")
async def put_config(body: VaultConfig, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.vault.set_path(user.id, body.path)


@router.get("/runs")
async def vault_runs(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    status = await ctx.vault.status(user.id)
    return {"vault": status["path"], "runs": await ctx.vault.list_runs(user.id)}


@router.get("/runs/{run_id}")
async def vault_run(run_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    record = await ctx.vault.read_run(user.id, run_id)
    if record is None:
        raise NotFound("run note not found in vault")
    return {"record": record.model_dump(), "tree": build_tree(record)}


@router.post("/reindex")
async def reindex(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.vault.reindex(user.id)
