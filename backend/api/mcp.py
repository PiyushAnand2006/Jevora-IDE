"""MCP drawer: manage servers (stdio / http / sse), health, tools and tool calls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser

router = APIRouter(prefix="/api/mcp/servers", tags=["mcp"])


class MCPServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: str = "stdio"
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    url: str | None = Field(default=None, max_length=1000)
    headers: dict[str, str] | None = None
    enabled: bool = True


class MCPPatch(BaseModel):
    enabled: bool


class ToolCallBody(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.get("")
async def list_servers(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return await ctx.mcp.list_servers(user.id)


@router.post("", status_code=201)
async def create_server(body: MCPServerBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.mcp.create(user.id, body.model_dump())


@router.patch("/{server_id}")
async def patch_server(server_id: str, body: MCPPatch, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.mcp.set_enabled(user.id, server_id, body.enabled)


@router.delete("/{server_id}")
async def delete_server(server_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.mcp.delete(user.id, server_id)
    return {"ok": True, "id": server_id}


@router.get("/{server_id}/health")
async def server_health(server_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.mcp.health(user.id, server_id)


@router.get("/{server_id}/tools")
async def server_tools(server_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return {"tools": await ctx.mcp.tools(user.id, server_id)}


@router.post("/{server_id}/tools/{tool_name}/call")
async def call_tool(server_id: str, tool_name: str, body: ToolCallBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.mcp.call(user.id, server_id, tool_name, body.arguments)
