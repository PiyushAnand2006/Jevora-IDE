"""Portable run records (DB ⇄ vault) and the parent→child call tree."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.ids import iso
from backend.db.models import AgentCall, Artifact, Decision, Run

PREVIEW_CHARS = 300


class CallRecord(BaseModel):
    call_id: str
    parent_call_id: str | None = None
    agent_id: str | None = None
    agent_name: str = ""
    role: str
    model_used: str | None = None
    provider_id: str | None = None
    category: str | None = None
    depth: int = 0
    status: str = "running"
    started_at: str | None = None
    ended_at: str | None = None
    tokens: int = 0
    cost: float = 0.0
    input: str = ""
    output: str = ""
    error: str | None = None
    lineage: list[str] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    id: str
    call_id: str | None = None
    type: str
    engine: str
    choice: str
    confidence: float
    alternatives: dict[str, float] = Field(default_factory=dict)
    latency_ms: float = 0.0
    fallback_used: bool = False
    shadow: bool = False
    acted: bool = True
    reason: str = ""
    created_at: str | None = None
    evidence_ref: list[str] = Field(default_factory=list)
    input_state: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None


class ArtifactRecord(BaseModel):
    id: str
    call_id: str | None = None
    kind: str
    path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class RunRecord(BaseModel):
    run_id: str
    user_id: str
    mode: str
    task: str
    status: str
    command: str | None = None
    agent_id: str | None = None
    model_override: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    final_output: str = ""
    iteration_count: int = 0
    total_tokens: int = 0
    cost_estimate: float = 0.0
    error: str | None = None
    calls: list[CallRecord] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    diff: str = ""


def _ms(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((b - a).total_seconds() * 1000, 1)


def call_from_row(row: AgentCall) -> CallRecord:
    return CallRecord(
        call_id=row.id, parent_call_id=row.parent_call_id, agent_id=row.agent_id, agent_name=row.agent_name,
        role=row.role, model_used=row.model_used, provider_id=row.provider_id, category=row.category,
        depth=row.depth, status=row.status, started_at=iso(row.started_at), ended_at=iso(row.ended_at),
        tokens=row.tokens, cost=round(row.cost or 0.0, 6), input=row.input or "", output=row.output or "",
        error=row.error, lineage=list(row.lineage or []))


def decision_from_row(row: Decision) -> DecisionRecord:
    return DecisionRecord(
        id=row.id, call_id=row.call_id, type=row.type, engine=row.engine, choice=row.choice,
        confidence=round(row.confidence, 4), alternatives={k: round(float(v), 4) for k, v in
                                                           (row.alternatives or {}).items()},
        latency_ms=round(row.latency_ms or 0.0, 2), fallback_used=row.fallback_used, shadow=row.shadow,
        acted=row.acted, reason=row.reason or "", created_at=iso(row.created_at),
        evidence_ref=list(row.evidence_ref or []), input_state=dict(row.input_state_json or {}), label=row.label)


async def load_run_record(session: AsyncSession, run_id: str) -> RunRecord | None:
    run = await session.get(Run, run_id)
    if run is None:
        return None
    calls = list(await session.scalars(
        select(AgentCall).where(AgentCall.run_id == run_id).order_by(AgentCall.started_at, AgentCall.id)))
    decisions = list(await session.scalars(
        select(Decision).where(Decision.run_id == run_id).order_by(Decision.created_at, Decision.id)))
    artifacts = list(await session.scalars(
        select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at, Artifact.id)))
    diff = ""
    for art in artifacts:
        if art.kind == "diff":
            diff = str((art.meta or {}).get("diff", ""))
    return RunRecord(
        run_id=run.id, user_id=run.user_id, mode=run.mode, task=run.task, status=run.status, command=run.command,
        agent_id=run.agent_id, model_override=run.model_override, created_at=iso(run.created_at),
        started_at=iso(run.started_at), finished_at=iso(run.finished_at), final_output=run.final_output or "",
        iteration_count=run.iteration_count, total_tokens=run.total_tokens,
        cost_estimate=round(run.cost_estimate or 0.0, 6), error=run.error,
        calls=[call_from_row(c) for c in calls],
        decisions=[decision_from_row(d) for d in decisions],
        artifacts=[ArtifactRecord(id=a.id, call_id=a.call_id, kind=a.kind, path=a.path,
                                  meta={k: v for k, v in (a.meta or {}).items() if k != "diff"},
                                  created_at=iso(a.created_at)) for a in artifacts],
        diff=diff)


def _decision_brief(d: DecisionRecord) -> dict[str, Any]:
    return {"id": d.id, "type": d.type, "choice": d.choice, "confidence": d.confidence, "engine": d.engine,
            "shadow": d.shadow, "acted": d.acted, "fallback_used": d.fallback_used}


def build_tree(record: RunRecord) -> dict[str, Any]:
    """Nested parent→child call tree with model, status, timing and decisions per node."""
    by_call: dict[str, list[DecisionRecord]] = {}
    run_level: list[DecisionRecord] = []
    for decision in record.decisions:
        if decision.call_id:
            by_call.setdefault(decision.call_id, []).append(decision)
        else:
            run_level.append(decision)
    nodes: dict[str, dict[str, Any]] = {}
    for call in record.calls:
        nodes[call.call_id] = {
            "call_id": call.call_id, "parent_call_id": call.parent_call_id, "agent_id": call.agent_id,
            "agent_name": call.agent_name, "role": call.role, "model_used": call.model_used,
            "provider_id": call.provider_id, "category": call.category, "depth": call.depth,
            "status": call.status, "started_at": call.started_at, "ended_at": call.ended_at,
            "duration_ms": _ms(call.started_at, call.ended_at), "tokens": call.tokens, "cost": call.cost,
            "error": call.error, "output_preview": call.output[:PREVIEW_CHARS],
            "decisions": [_decision_brief(d) for d in by_call.get(call.call_id, [])],
            "children": [],
        }
    roots: list[dict[str, Any]] = []
    for call in record.calls:
        node = nodes[call.call_id]
        parent = nodes.get(call.parent_call_id or "")
        (parent["children"] if parent is not None else roots).append(node)
    # Decisions referring to unknown calls are surfaced at run level rather than dropped.
    for call_id, items in by_call.items():
        if call_id not in nodes:
            run_level.extend(items)
    return {
        "run_id": record.run_id, "status": record.status, "mode": record.mode, "task": record.task,
        "created_at": record.created_at, "finished_at": record.finished_at,
        "agents": sum(1 for c in record.calls if c.depth == 0),
        "subagents": sum(1 for c in record.calls if c.depth > 0),
        "models_used": sorted({c.model_used for c in record.calls if c.model_used}),
        "roots": roots,
        "decisions": [_decision_brief(d) for d in run_level],
    }
