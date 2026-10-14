"""Agents: CRUD, templates, live status counters, run an agent, natural-language generation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.agents.templates import BUILTIN_TEMPLATES
from backend.api.deps import Ctx, CurrentUser

router = APIRouter(tags=["agents"])


class AgentBody(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    template: str | None = None
    system_prompt: str | None = Field(default=None, max_length=20_000)
    tool_permissions: list[str] | None = None
    model_binding: dict[str, Any] | None = None
    memory_scope: str | None = None
    max_depth: int | None = None
    max_fanout: int | None = None
    budget: dict[str, Any] | None = None
    temperature: float | None = None


class AgentRunBody(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    mode: str = "build"
    model: str | None = None
    attachments: list[str] = Field(default_factory=list)


class GenerateBody(BaseModel):
    description: str = Field(min_length=3, max_length=8000)
    save: bool = True


@router.get("/api/agents/status")
async def agents_status(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    counts = ctx.agent_pool.status(user.id)
    vms = await ctx.sandboxes.list_vms(user.id)
    return {**counts, "vms": vms["running"], "vms_total": len(vms["vms"]),
            "runs_active": ctx.executor.active_count, "runs_queued": ctx.executor.queued_count,
            "sandbox": vms["sandbox"]["kind"]}


@router.get("/api/agents/templates")
async def agent_templates(user: CurrentUser) -> list[dict[str, Any]]:
    return [{"id": key, **cfg.public()} for key, cfg in BUILTIN_TEMPLATES.items()]


@router.get("/api/agents")
async def list_agents(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return await ctx.agents.list(user.id)


@router.post("/api/agents", status_code=201)
async def create_agent(body: AgentBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    agent = await ctx.agents.create(user.id, body.model_dump(exclude_none=True))
    return await ctx.agents.public(agent)


@router.post("/api/agents/generate", status_code=201)
async def generate_agent(body: GenerateBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.agent_generator.generate(user.id, body.description, save=body.save)


@router.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.agents.public(await ctx.agents.get(user.id, agent_id))


@router.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    agent = await ctx.agents.update(user.id, agent_id, body.model_dump(exclude_none=True))
    return await ctx.agents.public(agent)


@router.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.agents.delete(user.id, agent_id)
    return {"ok": True, "id": agent_id}


@router.post("/api/agents/{agent_id}/run", status_code=201)
async def run_agent(agent_id: str, body: AgentRunBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.agents.get(user.id, agent_id)
    run = await ctx.runs.create_from_request(user.id, {**body.model_dump(), "agent_id": agent_id})
    return ctx.runs.serialize(run, [], [], [], None)
