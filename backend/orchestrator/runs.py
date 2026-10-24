"""Run service: create/list/get/delete runs, call tree, cancel/resume/escalation answers."""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversations import owned_conversation, save_message
from backend.core.errors import AppError, Conflict, NotFound
from backend.core.events import EventBus
from backend.core.ids import iso, new_id, utcnow
from backend.db.models import AgentCall, Artifact, Conversation, Decision, EscalationRequest, Run
from backend.orchestrator.approvals import ApprovalBroker
from backend.orchestrator.executor import RunExecutor
from backend.orchestrator.recorder import RunRecorder
from backend.orchestrator.records import build_tree, load_run_record
from backend.vault.service import VaultService

MODES = ("build", "chat", "research", "analysis")
MODE_ALIASES = {"general": "research"}


def encode_cursor(created_at: str, run_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at, run_id]).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, run_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(created_at), str(run_id)
    except (ValueError, TypeError) as exc:
        raise AppError("invalid cursor", code="invalid_cursor", status_code=422) from exc


def _decision_public(d: Decision) -> dict[str, Any]:
    return {"id": d.id, "type": d.type, "choice": d.choice, "confidence": round(d.confidence, 4),
            "engine": d.engine, "call_id": d.call_id, "shadow": d.shadow, "acted": d.acted,
            "fallback_used": d.fallback_used, "reason": d.reason, "latency_ms": round(d.latency_ms, 2),
            "label": d.label, "created_at": iso(d.created_at)}


class RunService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventBus, executor: RunExecutor,
                 vault: VaultService, max_prompt_chars: int, approvals: ApprovalBroker) -> None:
        self.sessions = sessions
        self.approvals = approvals
        self.delete_hooks: list[Any] = []
        self.events = events
        self.executor = executor
        self.vault = vault
        self.max_prompt_chars = max_prompt_chars

    # ------------------------------------------------------------------ create
    command_parser: Any = None  # set by the command registry: (text, mode) -> (command, mode, text)

    async def create_from_request(self, user_id: str, body: dict[str, Any]) -> Run:
        text = str(body.get("text") or "")
        mode = str(body.get("mode") or "build")
        command: str | None = None
        if self.command_parser is not None and text.lstrip().startswith("/"):
            command, mode, text = self.command_parser(text, mode)
        return await self.create(user_id, text=text, mode=mode, model=body.get("model"),
                                 attachments=body.get("attachments") or [], agent_id=body.get("agent_id"),
                                 conversation_id=body.get("conversation_id"),
                                 workspace_id=body.get("workspace_id"), command=command,
                                 meta={**(body.get("meta") or {}), "web_search": bool(body.get("web_search", False))})

    async def create(self, user_id: str, *, text: str, mode: str, model: str | None = None,
                     attachments: list[str] | None = None, agent_id: str | None = None,
                     conversation_id: str | None = None, workspace_id: str | None = None,
                     command: str | None = None, meta: dict[str, Any] | None = None) -> Run:
        mode = MODE_ALIASES.get((mode or "build").lower(), (mode or "build").lower())
        if mode not in MODES:
            raise AppError(f"mode must be one of {', '.join(MODES)}", code="invalid_mode", status_code=422)
        text = (text or "").strip()
        if not text and not attachments:
            raise AppError("text must not be empty", code="empty_prompt", status_code=422)
        if len(text) > self.max_prompt_chars:
            raise AppError(f"prompt is too long ({len(text)} > {self.max_prompt_chars} characters)",
                           code="prompt_too_long", status_code=413)
        if not text:
            text = "Review the attached context."
        run = Run(id=new_id("run_"), user_id=user_id, mode=mode, task=text, command=command,
                  model_override=None if (model or "auto").lower() == "auto" else model,
                  attachments=list(attachments or []), agent_id=agent_id, conversation_id=conversation_id,
                  workspace_id=workspace_id, status="queued", meta=meta or {})
        async with self.sessions() as session:
            conversation = None
            if conversation_id is not None:
                conversation = await owned_conversation(session, user_id, conversation_id, mode)
            elif mode == "chat":
                conversation = Conversation(user_id=user_id, title=text.splitlines()[0][:120], mode=mode)
                session.add(conversation)
                await session.flush()
            if conversation is not None:
                run.conversation_id = conversation.id
                conversation.updated_at = utcnow()
                if mode == "chat":
                    await save_message(session, conversation, run.id, "user", text)
            session.add(run)
            await session.commit()
            await session.refresh(run)
        self.events.publish("run.created", user_id=user_id, run_id=run.id,
                            data={"mode": mode, "task": text[:300], "agent_id": agent_id,
                                  "conversation_id": run.conversation_id})
        self.events.publish("run.status", user_id=user_id, run_id=run.id, data={"status": "queued", "mode": mode})
        self.executor.submit(run.id, user_id)
        return run

    # ------------------------------------------------------------------ read
    async def _get_owned(self, session: AsyncSession, user_id: str, run_id: str) -> Run:
        run = await session.get(Run, run_id)
        if run is None or run.user_id != user_id:
            raise NotFound("run not found")
        return run

    async def get_public(self, user_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            run = await self._get_owned(session, user_id, run_id)
            decisions = list(await session.scalars(
                select(Decision).where(Decision.run_id == run_id).order_by(Decision.created_at, Decision.id)))
            calls = list(await session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)))
            artifacts = list(await session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)))
            escalation = await session.scalar(select(EscalationRequest).where(
                EscalationRequest.run_id == run_id, EscalationRequest.status == "open")
                .order_by(EscalationRequest.created_at.desc()))
        return self.serialize(run, decisions, calls, artifacts, escalation)

    @staticmethod
    def serialize(run: Run, decisions: list[Decision], calls: list[AgentCall], artifacts: list[Artifact],
                  escalation: EscalationRequest | None) -> dict[str, Any]:
        acted = [d for d in decisions if d.acted]
        return {
            "id": run.id, "status": run.status, "mode": run.mode, "task": run.task, "text": run.task,
            "command": run.command, "model": run.model_override or "auto", "agent_id": run.agent_id,
            "conversation_id": run.conversation_id, "workspace_id": run.workspace_id,
            "attachments": run.attachments or [], "created_at": iso(run.created_at),
            "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "final_output": run.final_output or "", "error": run.error, "cancelled": run.cancelled,
            "iteration_count": run.iteration_count, "total_tokens": run.total_tokens,
            "cost_estimate": round(run.cost_estimate or 0.0, 6), "vault_note_path": run.vault_note_path,
            "decisions": [_decision_public(d) for d in acted],
            "shadow_decisions": [_decision_public(d) for d in decisions if not d.acted],
            "agents": {"total": sum(1 for c in calls if c.depth == 0),
                       "subagents": sum(1 for c in calls if c.depth > 0),
                       "running": sum(1 for c in calls if c.status == "running")},
            "models_used": sorted({c.model_used for c in calls if c.model_used}),
            "artifacts": [{"id": a.id, "kind": a.kind, "path": a.path, "call_id": a.call_id,
                           "meta": {k: v for k, v in (a.meta or {}).items() if k not in ("diff", "content")},
                           "created_at": iso(a.created_at)} for a in artifacts],
            "escalation": None if escalation is None else {
                "id": escalation.id, "kind": escalation.kind, "question": escalation.question,
                "options": escalation.options, "context": escalation.context,
                "created_at": iso(escalation.created_at)},
            "meta": run.meta or {},
        }

    async def list(self, user_id: str, *, limit: int = 20, cursor: str | None = None, mode: str | None = None,
                   status: str | None = None, q: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        stmt = select(Run).where(Run.user_id == user_id)
        if mode:
            stmt = stmt.where(Run.mode == MODE_ALIASES.get(mode, mode))
        if status:
            stmt = stmt.where(Run.status.in_(status.split(",")))
        if agent_id:
            stmt = stmt.where(Run.agent_id == agent_id)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(or_(Run.task.ilike(f"%{escaped}%", escape="\\"),
                                  Run.final_output.ilike(f"%{escaped}%", escape="\\")))
        if cursor:
            created_at, run_id = decode_cursor(cursor)
            from datetime import datetime

            stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            stmt = stmt.where(or_(Run.created_at < stamp, and_(Run.created_at == stamp, Run.id < run_id)))
        stmt = stmt.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
        async with self.sessions() as session:
            runs = list(await session.scalars(stmt))
            page = runs[:limit]
            ids = [r.id for r in page]
            verdicts: dict[str, Decision] = {}
            if ids:
                for d in await session.scalars(select(Decision).where(Decision.run_id.in_(ids), Decision.acted)
                                               .order_by(Decision.created_at, Decision.id)):
                    if d.run_id:
                        verdicts[d.run_id] = d
        items = []
        for run in page:
            verdict = verdicts.get(run.id)
            items.append({
                "id": run.id, "title": run.task.strip().splitlines()[0][:120] if run.task.strip() else "",
                "task": run.task[:500], "mode": run.mode, "status": run.status, "agent_id": run.agent_id,
                "created_at": iso(run.created_at), "finished_at": iso(run.finished_at),
                "total_tokens": run.total_tokens, "vault_note_path": run.vault_note_path,
                "verdict": None if verdict is None else {"choice": verdict.choice, "engine": verdict.engine,
                                                         "confidence": round(verdict.confidence, 4)},
            })
        next_cursor = encode_cursor(iso(page[-1].created_at) or "", page[-1].id) if len(runs) > limit else None
        return {"items": items, "next_cursor": next_cursor}

    async def tree(self, user_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            await self._get_owned(session, user_id, run_id)
            record = await load_run_record(session, run_id)
        assert record is not None
        return build_tree(record)

    async def diff(self, user_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            await self._get_owned(session, user_id, run_id)
            art = await session.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == "diff"))
        meta = dict(art.meta or {}) if art is not None else {}
        return {"run_id": run_id, "diff": meta.pop("diff", ""), "stats": meta}

    # ------------------------------------------------------------------ mutations
    async def delete(self, user_id: str, run_id: str) -> None:
        async with self.sessions() as session:
            await self._get_owned(session, user_id, run_id)
        if self.executor.is_active(run_id):
            await self.executor.cancel(run_id)
        async with self.sessions() as session:
            await session.execute(delete(Run).where(Run.id == run_id))
            await session.commit()
        await self.vault.delete_run(user_id, run_id)
        for hook in self.delete_hooks:
            await hook(run_id)
        self.events.publish("run.deleted", user_id=user_id, run_id=run_id, data={})
        self.events.forget_run(run_id)

    async def cancel(self, user_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            run = await self._get_owned(session, user_id, run_id)
            status = run.status
        if self.executor.is_active(run_id):
            await self.executor.cancel(run_id)
        elif status not in ("done", "failed"):
            recorder = RunRecorder(self.sessions, self.events, run_id, user_id)
            await recorder.cancel_open_calls("cancelled by user")
            await recorder.set_status("failed", error="cancelled by user", cancelled=True,
                                      final_output="Run cancelled by user.")
            await self.vault.write_run(run_id)
        async with self.sessions() as session:
            for esc in await session.scalars(select(EscalationRequest).where(
                    EscalationRequest.run_id == run_id, EscalationRequest.status == "open")):
                esc.status = "cancelled"
                esc.resolved_at = utcnow()
            await session.commit()
        return await self.get_public(user_id, run_id)

    async def resume(self, user_id: str, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            run = await self._get_owned(session, user_id, run_id)
            status, cancelled = run.status, run.cancelled
        if self.executor.is_active(run_id):
            raise Conflict("run is already executing", code="run_active")
        if status == "done" or cancelled:
            raise Conflict(f"run cannot be resumed from status '{status}'", code="not_resumable")
        if status == "escalated":
            return await self.answer_escalation(user_id, run_id, choice="retry", message="resumed by user")
        self.executor.submit(run_id, user_id, resume="restart")
        return await self.get_public(user_id, run_id)

    async def answer_escalation(self, user_id: str, run_id: str, *, choice: str, message: str | None = None,
                                escalation_id: str | None = None) -> dict[str, Any]:
        async with self.sessions() as session:
            await self._get_owned(session, user_id, run_id)
            stmt = select(EscalationRequest).where(EscalationRequest.run_id == run_id,
                                                   EscalationRequest.status == "open")
            if escalation_id:
                stmt = stmt.where(EscalationRequest.id == escalation_id)
            esc = await session.scalar(stmt.order_by(EscalationRequest.created_at.desc()))
            if esc is None:
                raise NotFound("no open escalation for this run", code="no_escalation")
            if esc.options and choice not in esc.options:
                raise AppError(f"choice must be one of {esc.options}", code="invalid_choice", status_code=422)
            esc.status = "resolved"
            esc.resolved_choice = choice
            esc.resolved_message = message
            esc.resolved_at = utcnow()
            if esc.decision_id:
                decision = await session.get(Decision, esc.decision_id)
                if decision is not None and decision.label is None:
                    # The user's answer becomes labelled training data for the decision layer.
                    decision.label = _label_from_answer(choice, decision.type)
                    decision.label_note = f"escalation answer: {choice}" + (f" — {message}" if message else "")
                    decision.labelled_at = utcnow()
            esc_kind = esc.kind
            esc_id = esc.id
            await session.commit()
        self.events.publish("escalation.resolved", user_id=user_id, run_id=run_id,
                            data={"id": esc_id, "choice": choice, "message": message})
        payload = {"escalation_id": esc_id, "kind": esc_kind, "choice": choice, "message": message or ""}
        if self.approvals.resolve(esc_id, payload):
            pass  # a live in-run approval wait (awaiting_user) consumed the answer
        elif not self.executor.is_active(run_id):
            # Paused run (escalated, or awaiting_user across a restart): resume from its checkpoint.
            self.executor.submit(run_id, user_id, resume=payload)
        return await self.get_public(user_id, run_id)


def _label_from_answer(choice: str, decision_type: str) -> str:
    if decision_type == "verification":
        return {"accept": "confirm_done", "approve": "confirm_done", "retry": "restart_same_agent",
                "reassign": "reassign_new_agent", "abort": "escalate", "reject": "restart_same_agent"}.get(
            choice, choice)
    return choice
