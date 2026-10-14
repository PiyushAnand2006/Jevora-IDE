"""RunRecorder: the single write path for run state (DB rows + typed events)."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.events import TERMINAL_STATUSES, EventBus
from backend.core.ids import new_id, utcnow
from backend.core.logging import get_logger
from backend.core.metrics import Metrics
from backend.db.models import AgentCall, Artifact, Decision, EscalationRequest, Run

log = get_logger(__name__)

RUN_STATUSES = ("queued", "planning", "running", "verifying", "awaiting_user", "done", "failed", "escalated")


class RunRecorder:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventBus, run_id: str, user_id: str,
                 metrics: Metrics | None = None) -> None:
        self.sessions = sessions
        self.events = events
        self.run_id = run_id
        self.user_id = user_id
        self.metrics = metrics
        self._usage_lock = asyncio.Lock()

    # ------------------------------------------------------------------ events
    def emit(self, type: str, data: dict[str, Any] | None = None, *, call_id: str | None = None,
             parent_call_id: str | None = None) -> None:
        self.events.publish(type, user_id=self.user_id, run_id=self.run_id, call_id=call_id,
                            parent_call_id=parent_call_id, data=data or {})

    # ------------------------------------------------------------------ run status
    async def set_status(self, status: str, **fields: Any) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {status}")
        values: dict[str, Any] = {"status": status, **fields}
        now = utcnow()
        async with self.sessions() as session:
            run = await session.get(Run, self.run_id)
            if run is None:
                return
            if status in ("planning", "running") and run.started_at is None:
                values["started_at"] = now
            if status in TERMINAL_STATUSES:
                values.setdefault("finished_at", now)
            elif status not in ("awaiting_user",):
                values.setdefault("finished_at", None)
            for key, value in values.items():
                setattr(run, key, value)
            await session.commit()
            payload = {"status": status, "mode": run.mode, "iteration_count": run.iteration_count}
            if status in TERMINAL_STATUSES:
                payload.update({"final_output": run.final_output, "error": run.error})
        self.emit("run.status", payload)

    async def update_run(self, **fields: Any) -> None:
        async with self.sessions() as session:
            await session.execute(update(Run).where(Run.id == self.run_id).values(**fields))
            await session.commit()

    async def add_usage(self, tokens: int, cost: float) -> None:
        async with self._usage_lock, self.sessions() as session:
            await session.execute(update(Run).where(Run.id == self.run_id).values(
                total_tokens=Run.total_tokens + tokens, cost_estimate=Run.cost_estimate + cost))
            await session.commit()

    # ------------------------------------------------------------------ calls
    async def start_call(self, *, role: str, agent_name: str, task: str, parent_call_id: str | None = None,
                         depth: int = 0, agent_id: str | None = None, category: str | None = None,
                         lineage: list[str] | None = None, call_id: str | None = None) -> str:
        call_id = call_id or new_id("call_")
        async with self.sessions() as session:
            session.add(AgentCall(id=call_id, run_id=self.run_id, parent_call_id=parent_call_id, agent_id=agent_id,
                                  agent_name=agent_name, role=role, depth=depth, status="running", input=task,
                                  category=category, lineage=lineage or []))
            await session.commit()
        data = {"role": role, "agent_name": agent_name, "depth": depth, "category": category,
                "task": task[:500], "lineage": lineage or []}
        if parent_call_id is not None:
            self.emit("subagent.spawned", data, call_id=call_id, parent_call_id=parent_call_id)
        self.emit("agent.started", data, call_id=call_id, parent_call_id=parent_call_id)
        return call_id

    async def set_call_model(self, call_id: str, model: str | None, provider_id: str | None) -> None:
        async with self.sessions() as session:
            await session.execute(update(AgentCall).where(AgentCall.id == call_id)
                                  .values(model_used=model, provider_id=provider_id))
            await session.commit()

    async def add_call_usage(self, call_id: str, tokens: int, cost: float) -> None:
        async with self.sessions() as session:
            await session.execute(update(AgentCall).where(AgentCall.id == call_id).values(
                tokens=AgentCall.tokens + tokens, cost=AgentCall.cost + cost))
            await session.commit()

    async def finish_call(self, call_id: str, *, status: str, output: str = "", error: str | None = None) -> None:
        async with self.sessions() as session:
            call = await session.get(AgentCall, call_id)
            if call is None:
                return
            call.status = status
            call.output = output
            call.error = error
            call.ended_at = utcnow()
            parent = call.parent_call_id
            data = {"status": status, "role": call.role, "agent_name": call.agent_name, "model": call.model_used,
                    "tokens": call.tokens, "output_preview": output[:500], "error": error, "depth": call.depth}
            await session.commit()
        self.emit("agent.failed" if status in ("failed", "cancelled") else "agent.finished", data,
                  call_id=call_id, parent_call_id=parent)

    async def cancel_open_calls(self, reason: str = "cancelled") -> int:
        async with self.sessions() as session:
            result = await session.execute(
                update(AgentCall).where(AgentCall.run_id == self.run_id, AgentCall.status == "running")
                .values(status="cancelled", ended_at=utcnow(), error=reason))
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ decisions / artifacts
    async def record_decision(self, *, type: str, engine: str, choice: str, confidence: float,
                              alternatives: dict[str, float], latency_ms: float, input_state: dict[str, Any],
                              evidence_ref: list[str] | None = None, call_id: str | None = None,
                              fallback_used: bool = False, shadow: bool = False, acted: bool = True,
                              reason: str = "") -> str:
        decision_id = new_id("dec_")
        async with self.sessions() as session:
            session.add(Decision(
                id=decision_id, run_id=self.run_id, call_id=call_id, type=type, engine=engine,
                input_state_json=input_state, evidence_ref=evidence_ref or [], choice=choice,
                confidence=float(confidence), alternatives=alternatives, latency_ms=latency_ms,
                fallback_used=fallback_used, shadow=shadow, acted=acted, reason=reason))
            await session.commit()
        if self.metrics is not None:
            self.metrics.decisions.labels(type, engine, choice).inc()
            self.metrics.decision_latency.labels(engine).observe(latency_ms / 1000)
        self.emit("decision.made", {"id": decision_id, "type": type, "engine": engine, "choice": choice,
                                    "confidence": round(float(confidence), 4), "alternatives": alternatives,
                                    "latency_ms": round(latency_ms, 2), "shadow": shadow, "acted": acted,
                                    "fallback_used": fallback_used, "reason": reason}, call_id=call_id)
        return decision_id

    async def add_artifact(self, kind: str, *, path: str | None = None, meta: dict[str, Any] | None = None,
                           call_id: str | None = None) -> str:
        artifact_id = new_id("art_")
        async with self.sessions() as session:
            session.add(Artifact(id=artifact_id, run_id=self.run_id, call_id=call_id, kind=kind, path=path,
                                 meta=meta or {}))
            await session.commit()
        brief = {k: v for k, v in (meta or {}).items() if k not in ("diff", "content", "output")}
        self.emit("artifact.created", {"id": artifact_id, "kind": kind, "path": path, "meta": brief},
                  call_id=call_id)
        return artifact_id

    async def upsert_diff(self, diff: str, stats: dict[str, Any]) -> str:
        async with self.sessions() as session:
            existing = await session.scalar(select(Artifact).where(Artifact.run_id == self.run_id,
                                                                   Artifact.kind == "diff"))
            if existing is not None:
                existing.meta = {"diff": diff, **stats}
                await session.commit()
                self.emit("artifact.created", {"id": existing.id, "kind": "diff", "meta": stats})
                return existing.id
        return await self.add_artifact("diff", meta={"diff": diff, **stats})

    # ------------------------------------------------------------------ escalations
    async def open_escalation(self, *, question: str, options: list[str], kind: str = "decision",
                              context: dict[str, Any] | None = None, call_id: str | None = None,
                              decision_id: str | None = None) -> str:
        esc_id = new_id("esc_")
        async with self.sessions() as session:
            session.add(EscalationRequest(id=esc_id, run_id=self.run_id, call_id=call_id, decision_id=decision_id,
                                          kind=kind, question=question, options=options, context=context or {}))
            await session.commit()
        self.emit("escalation.requested", {"id": esc_id, "kind": kind, "question": question, "options": options,
                                           "context": context or {}}, call_id=call_id)
        return esc_id
