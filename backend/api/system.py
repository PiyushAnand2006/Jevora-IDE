"""System endpoints: health, feature flags, Prometheus metrics, token/cost usage."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import case, func, select, text

from backend import __version__
from backend.api.deps import Ctx, CurrentUser
from backend.app_context import AppContext
from backend.core.ids import iso, utcnow
from backend.db.models import LLMCall

router = APIRouter(tags=["system"])


async def _provider_reachability(ctx: AppContext, user_id: str) -> list[dict[str, Any]]:
    providers = await ctx.providers.list_for(user_id)

    async def probe(p: Any) -> dict[str, Any]:
        adapter = ctx.providers.adapter_for(p)
        try:
            models = await asyncio.wait_for(adapter.list_models(), timeout=4)
            return {"id": p.id, "name": p.name, "kind": p.kind, "reachable": True, "models": len(models),
                    "circuit": ctx.router.breakers.get(p.id).state}
        except (Exception, TimeoutError) as exc:
            return {"id": p.id, "name": p.name, "kind": p.kind, "reachable": False,
                    "error": str(exc)[:200] or type(exc).__name__, "circuit": ctx.router.breakers.get(p.id).state}

    return list(await asyncio.gather(*(probe(p) for p in providers)))


async def build_health(ctx: AppContext, user_id: str, probe_providers: bool = True) -> dict[str, Any]:
    db_ok = True
    try:
        async with ctx.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    health: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "time": iso(utcnow()),
        "database": {"ok": db_ok},
        "auth_required": ctx.settings.auth_required,
        "providers": await _provider_reachability(ctx, user_id) if probe_providers else [],
    }
    for name, component in ctx.extras.get("health_components", {}).items():
        try:
            health[name] = await component(user_id)
        except Exception as exc:  # a broken component must not break health
            health[name] = {"ok": False, "error": str(exc)[:300]}
    return health


@router.get("/api/system/health")
async def system_health(user: CurrentUser, ctx: Ctx, probe: bool = Query(True)) -> dict[str, Any]:
    return await build_health(ctx, user.id, probe_providers=probe)


@router.get("/api/features")
async def features(ctx: Ctx) -> dict[str, Any]:
    flags: dict[str, Any] = {
        "web_search": ctx.settings.web_search_enabled,
        "web_search_provider": ctx.settings.web_search_provider if ctx.settings.web_search_enabled else None,
        "auth_required": ctx.settings.auth_required,
        "oauth": ctx.settings.oauth_configured,
        "desktop_control": ctx.settings.desktop_control_enabled,
        "local_unsafe_exec": ctx.settings.local_unsafe_exec,
        "approval_policy": ctx.settings.approval_policy,
    }
    flags.update(ctx.extras.get("feature_flags", {}))
    return flags


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(ctx: Ctx) -> PlainTextResponse:
    return PlainTextResponse(ctx.metrics.render().decode("utf-8"),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/usage")
async def usage(user: CurrentUser, ctx: Ctx, days: int = Query(30, ge=1, le=365),
                group_by: str = Query("model", pattern="^(model|provider|day|category)$")) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    column = {
        "model": LLMCall.model,
        "provider": LLMCall.provider_kind,
        "category": LLMCall.category,
        "day": func.date(LLMCall.created_at),
    }[group_by]
    async with ctx.sessions() as session:
        rows = await session.execute(
            select(column.label("key"), func.count(LLMCall.id), func.sum(LLMCall.prompt_tokens),
                   func.sum(LLMCall.completion_tokens), func.sum(LLMCall.cost_usd), func.avg(LLMCall.latency_ms),
                   func.sum(case((LLMCall.status == "error", 1), else_=0)))
            .where(LLMCall.user_id == user.id, LLMCall.created_at >= since)
            .group_by(column).order_by(func.sum(LLMCall.cost_usd).desc())
        )
        groups = [{"key": key, "calls": calls, "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0),
                   "cost_usd": round(float(cost or 0), 6), "avg_latency_ms": round(float(lat or 0), 1),
                   "errors": int(errs or 0)} for key, calls, pt, ct, cost, lat, errs in rows.all()]
    return {
        "since": iso(since), "group_by": group_by, "groups": groups,
        "totals": {
            "calls": sum(g["calls"] for g in groups),
            "tokens": sum(g["prompt_tokens"] + g["completion_tokens"] for g in groups),
            "cost_usd": round(sum(g["cost_usd"] for g in groups), 6),
        },
    }


async def ping_url(client: httpx.AsyncClient, url: str) -> bool:
    try:
        response = await client.get(url, timeout=2)
        return response.status_code < 500
    except httpx.HTTPError:
        return False
