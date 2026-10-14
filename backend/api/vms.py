"""Sandbox environments ("VMs"): list, create, start/stop, select, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser

router = APIRouter(prefix="/api/vms", tags=["vms"])


class VMCreate(BaseModel):
    name: str = Field(default="Sandbox", max_length=120)
    network: bool = False


@router.get("")
async def list_vms(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.sandboxes.list_vms(user.id)


@router.post("", status_code=201)
async def create_vm(body: VMCreate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.sandboxes.create_vm(user.id, body.name, body.network)


@router.post("/{vm_id}/start")
async def start_vm(vm_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.sandboxes.start_vm(user.id, vm_id, ctx.settings.workspace_root)


@router.post("/{vm_id}/stop")
async def stop_vm(vm_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.sandboxes.stop_vm(user.id, vm_id)


@router.post("/{vm_id}/select")
async def select_vm(vm_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.sandboxes.select_vm(user.id, vm_id)


@router.delete("/{vm_id}")
async def delete_vm(vm_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.sandboxes.delete_vm(user.id, vm_id)
    return {"ok": True, "id": vm_id}
