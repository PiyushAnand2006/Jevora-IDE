"""Saved agents (user-defined AgentConfigs): CRUD, validation and live status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import func, select

from backend.agents.config import AgentConfig
from backend.agents.templates import BUILTIN_TEMPLATES, template
from backend.core.errors import AppError, Conflict, NotFound
from backend.core.ids import iso, utcnow
from backend.db.models import Agent, Provider, Run

if TYPE_CHECKING:
    from backend.app_context import AppContext

ACTIVE = ("queued", "planning", "running", "verifying", "awaiting_user")


def build_config(payload: dict[str, Any]) -> AgentConfig:
    """Merge a (partial) config onto its template and validate the result."""
    base_name = payload.get("template")
    if base_name and base_name in BUILTIN_TEMPLATES:
        merged = template(base_name).model_dump()
    else:
        merged = template("coder").model_dump() if not payload.get("system_prompt") else {}
        merged.update({"template": None})
    for key, value in payload.items():
        if value is not None:
            merged[key] = value
    try:
        return AgentConfig.model_validate(merged)
    except ValidationError as exc:
        raise AppError(f"invalid agent config: {exc.errors()[0].get('msg')} at "
                       f"{'.'.join(str(p) for p in exc.errors()[0].get('loc', []))}",
                       code="invalid_agent_config", status_code=422, errors=exc.errors(include_url=False)) from exc


class AgentService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def _check_binding(self, user_id: str, config: AgentConfig) -> None:
        pid = config.model_binding.provider_id
        if config.model_binding.mode == "explicit" and pid:
            async with self.ctx.sessions() as session:
                provider = await session.get(Provider, pid)
            if provider is None or provider.user_id != user_id:
                raise AppError(f"model_binding.provider_id '{pid}' is not one of your providers",
                               code="invalid_binding", status_code=422)

    async def public(self, agent: Agent) -> dict[str, Any]:
        async with self.ctx.sessions() as session:
            active = await session.scalar(select(func.count(Run.id)).where(
                Run.agent_id == agent.id, Run.status.in_(ACTIVE)))
            last = await session.scalar(select(Run).where(Run.agent_id == agent.id)
                                        .order_by(Run.created_at.desc()).limit(1))
        return {
            "id": agent.id, "name": agent.name, "description": agent.description, "template": agent.template,
            "config": agent.config, "generated_by": agent.generated_by,
            "status": "running" if active else "idle", "active_runs": int(active or 0),
            "last_run": None if last is None else {"id": last.id, "status": last.status,
                                                   "created_at": iso(last.created_at)},
            "created_at": iso(agent.created_at), "updated_at": iso(agent.updated_at),
        }

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        async with self.ctx.sessions() as session:
            agents = list(await session.scalars(select(Agent).where(Agent.user_id == user_id)
                                                .order_by(Agent.created_at.desc())))
        return [await self.public(a) for a in agents]

    async def get(self, user_id: str, agent_id: str) -> Agent:
        async with self.ctx.sessions() as session:
            agent = await session.get(Agent, agent_id)
        if agent is None or agent.user_id != user_id:
            raise NotFound("agent not found")
        return agent

    async def config(self, user_id: str, agent_id: str) -> AgentConfig:
        return AgentConfig.model_validate((await self.get(user_id, agent_id)).config)

    async def create(self, user_id: str, payload: dict[str, Any], generated_by: str = "user") -> Agent:
        config = build_config(payload)
        await self._check_binding(user_id, config)
        async with self.ctx.sessions() as session:
            clash = await session.scalar(select(Agent).where(Agent.user_id == user_id, Agent.name == config.name))
            if clash is not None:
                raise Conflict(f"an agent named '{config.name}' already exists", code="duplicate_agent",
                               existing_id=clash.id)
            agent = Agent(user_id=user_id, name=config.name, description=config.description,
                          template=config.template, config=config.public(), generated_by=generated_by)
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
        self.ctx.events.publish("agents.status", user_id=user_id, data={"agent_created": agent.id})
        return agent

    async def update(self, user_id: str, agent_id: str, payload: dict[str, Any]) -> Agent:
        existing = await self.get(user_id, agent_id)
        merged = dict(existing.config)
        merged.update({k: v for k, v in payload.items() if v is not None})
        config = build_config(merged)
        await self._check_binding(user_id, config)
        async with self.ctx.sessions() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            if config.name != agent.name:
                clash = await session.scalar(select(Agent).where(
                    Agent.user_id == user_id, Agent.name == config.name, Agent.id != agent_id))
                if clash is not None:
                    raise Conflict(f"an agent named '{config.name}' already exists", code="duplicate_agent")
            agent.name, agent.description, agent.template = config.name, config.description, config.template
            agent.config = config.public()
            agent.updated_at = utcnow()
            await session.commit()
            await session.refresh(agent)
            return agent

    async def delete(self, user_id: str, agent_id: str) -> None:
        await self.get(user_id, agent_id)
        async with self.ctx.sessions() as session:
            agent = await session.get(Agent, agent_id)
            if agent is not None:
                await session.delete(agent)
                await session.commit()
