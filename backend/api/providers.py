"""Provider CRUD, per-provider model listing, merged model catalogue and router config."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import NotFound
from backend.providers.base import ProviderUnavailable
from backend.providers.catalog import UI_MODEL_ALIASES, is_chat_model
from backend.providers.service import provider_public

router = APIRouter(tags=["providers"])


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default="", max_length=4000)
    kind: str | None = "custom"


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    kind: str | None = None


@router.get("/api/providers")
async def list_providers(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return [provider_public(p) for p in await ctx.providers.list_for(user.id)]


@router.post("/api/providers", status_code=201)
async def create_provider(body: ProviderCreate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    provider = await ctx.providers.create(user.id, name=body.name, base_url=body.base_url,
                                          api_key=(body.api_key or "").strip() or None, kind=body.kind)
    return provider_public(provider)


@router.put("/api/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderUpdate, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    provider = await ctx.providers.update(
        user.id, provider_id, name=body.name, base_url=body.base_url,
        api_key=(body.api_key or "").strip() or None, kind=body.kind)
    return provider_public(provider)


@router.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    await ctx.providers.delete(user.id, provider_id)
    return {"ok": True, "id": provider_id}


@router.get("/api/providers/{provider_id}/models")
async def provider_models(provider_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    models, error = await ctx.providers.fetch_models(user.id, provider_id)
    body: dict[str, Any] = {"models": models}
    if error:
        body["error"] = error
    return body


@router.get("/api/models")
async def model_catalogue(user: CurrentUser, ctx: Ctx, refresh: bool = Query(False)) -> dict[str, Any]:
    errors: dict[str, str | None] = {}
    if refresh:
        errors = await ctx.providers.refresh_all(user.id)
    pairs = await ctx.providers.bindings(user.id)
    models = [{
        "id": f"{p.id}:{b.model}", "provider_id": p.id, "provider_name": p.name, "kind": p.kind,
        "model": b.model, "tier": b.tier, "capabilities": b.capabilities, "chat": is_chat_model(b.model),
    } for p, b in pairs]
    aliases: dict[str, Any] = {}
    for alias in UI_MODEL_ALIASES:
        try:
            binding = (await ctx.router.resolve(user.id, alias))[0]
            aliases[alias] = binding.public()
        except (NotFound, ProviderUnavailable) as exc:
            aliases[alias] = {"available": False, "reason": str(exc)}
    effective = await ctx.router.effective_mapping(user.id)
    return {
        "auto": {"description": "Decision-Engine routing by task category",
                 "categories": {cat: (chain[0].public() if chain else None) for cat, chain in effective.items()}},
        "aliases": aliases,
        "models": models,
        "errors": {k: v for k, v in errors.items() if v},
    }


class RouterConfigBody(BaseModel):
    mapping: dict[str, list[dict[str, str]]] = Field(default_factory=dict)


@router.get("/api/router/config")
async def get_router_config(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.router.get_config(user.id)


@router.put("/api/router/config")
async def put_router_config(body: RouterConfigBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.router.set_config(user.id, body.mapping)
