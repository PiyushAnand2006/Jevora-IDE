"""Slash commands, plugins and decision-layer endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.deps import Ctx, CurrentUser
from backend.core.errors import AppError, NotFound
from backend.core.ids import iso, utcnow
from backend.db.models import Decision, Run
from backend.decision.schemas import SCHEMA_CHOICES
from backend.orchestrator.commands import command_list

router = APIRouter(tags=["extras"])


@router.get("/api/commands")
async def commands(user: CurrentUser) -> list[dict[str, Any]]:
    return command_list()


# --------------------------------------------------------------------------- plugins
class PluginPatch(BaseModel):
    enabled: bool


@router.get("/api/plugins")
async def list_plugins(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return await ctx.plugins.list(user.id)


@router.patch("/api/plugins/{plugin_id}")
async def patch_plugin(plugin_id: str, body: PluginPatch, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return await ctx.plugins.set_enabled(user.id, plugin_id, body.enabled)


# --------------------------------------------------------------------------- decision layer
@router.get("/api/decision/config")
async def get_decision_config(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    return {"config": ctx.decisions.config.model_dump(), "status": await ctx.decisions.status()}


@router.put("/api/decision/config")
async def put_decision_config(body: dict[str, Any], user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    allowed = {"escalation_threshold", "thresholds", "validated", "shadow_enabled", "escalate_on_disagreement",
               "primary", "fallbacks"}
    unknown = set(body) - allowed
    if unknown:
        raise AppError(f"unknown config keys: {sorted(unknown)}", code="invalid_config", status_code=422)
    if "primary" in body and body["primary"] not in ("laya", "jev", "rules"):
        raise AppError("primary must be laya, jev or rules", code="invalid_config", status_code=422)
    try:
        config = await ctx.decisions.save_config(body)
    except ValueError as exc:
        raise AppError(f"invalid decision config: {exc}", code="invalid_config", status_code=422) from exc
    return {"config": config.model_dump(), "status": await ctx.decisions.status()}


def _decision_public(d: Decision) -> dict[str, Any]:
    return {"id": d.id, "run_id": d.run_id, "call_id": d.call_id, "type": d.type, "engine": d.engine,
            "choice": d.choice, "confidence": d.confidence, "alternatives": d.alternatives,
            "latency_ms": d.latency_ms, "fallback_used": d.fallback_used, "shadow": d.shadow, "acted": d.acted,
            "reason": d.reason, "label": d.label, "label_note": d.label_note, "created_at": iso(d.created_at)}


@router.get("/api/decisions")
async def list_decisions(user: CurrentUser, ctx: Ctx, run_id: str | None = None, type: str | None = None,
                         labelled: bool | None = None, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    stmt = select(Decision).join(Run, Run.id == Decision.run_id).where(Run.user_id == user.id)
    if run_id:
        stmt = stmt.where(Decision.run_id == run_id)
    if type:
        stmt = stmt.where(Decision.type == type)
    if labelled is not None:
        stmt = stmt.where(Decision.label.is_not(None) if labelled else Decision.label.is_(None))
    async with ctx.sessions() as session:
        rows = list(await session.scalars(stmt.order_by(Decision.created_at.desc()).limit(limit)))
    return {"items": [_decision_public(d) for d in rows]}


class EvalBody(BaseModel):
    engines: list[str] = Field(default_factory=lambda: ["rules", "laya", "jev"])
    promote: bool = False
    include_seed: bool = True


@router.post("/api/decision/eval")
async def run_decision_eval(body: EvalBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    """Evaluate engines on seed + labelled decisions; optionally apply Laya's promotion gate."""
    from backend.decision.eval.dataset import export_logged, load_jsonl, seed_dataset
    from backend.decision.eval.runner import render_markdown, run_eval

    datasets = ctx.settings.data_dir / "datasets"
    path, _ = await export_logged(ctx.sessions, datasets / "labelled-api.jsonl")
    items = (seed_dataset() if body.include_seed else []) + load_jsonl(path)
    report = await run_eval(ctx.decisions, items, body.engines, promote=body.promote)
    out = ctx.settings.data_dir / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return {"report": report.model_dump(), "markdown": render_markdown(report)}


@router.get("/api/decision/eval/latest")
async def latest_decision_eval(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    import json

    path = ctx.settings.data_dir / "eval" / "latest.json"
    if not path.exists():
        raise NotFound("no evaluation has been run yet")
    return dict(json.loads(path.read_text(encoding="utf-8")))


class LabelBody(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=4000)


@router.post("/api/decisions/{decision_id}/label")
async def label_decision(decision_id: str, body: LabelBody, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        decision = await session.get(Decision, decision_id)
        run = await session.get(Run, decision.run_id) if decision is not None and decision.run_id else None
        if decision is None or (run is not None and run.user_id != user.id):
            raise NotFound("decision not found")
        options = SCHEMA_CHOICES.get(decision.type, ())
        if body.label not in options:
            raise AppError(f"label must be one of {list(options)}", code="invalid_label", status_code=422)
        decision.label = body.label
        decision.label_note = body.note
        decision.labelled_at = utcnow()
        await session.commit()
        await session.refresh(decision)
        return _decision_public(decision)
