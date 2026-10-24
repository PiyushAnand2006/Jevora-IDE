"""Async application service around the synchronous Obsidian vault."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.core.events import EventBus
from backend.core.ids import new_id, utcnow
from backend.db.models import AgentCall, Artifact, Decision, Run, UserSetting
from backend.orchestrator.records import ArtifactRecord, RunRecord, load_run_record
from backend.vault.obsidian import Vault, VaultError

CONFIG_KEY = "vault_config"


def _datetime(value: str | None) -> datetime:
    if not value:
        return utcnow()
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class VaultService:
    """Coordinate vault IO, run snapshots, and DB reindexing."""

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession], events: EventBus) -> None:
        self.settings = settings
        self.sessions = sessions
        self.events = events
        self._base_path = settings.resolved_vault_path
        self._vaults: dict[str, Vault] = {}
        self.vault = Vault(self._base_path)

    @property
    def path(self) -> Path:
        return self.vault.path

    async def _path_for(self, user_id: str) -> Path:
        async with self.sessions() as session:
            row = await session.scalar(select(UserSetting).where(UserSetting.user_id == user_id,
                                                                 UserSetting.key == CONFIG_KEY))
        if row is not None and isinstance(row.value, dict) and isinstance(row.value.get("path"), str):
            return Path(row.value["path"])
        if user_id == "usr_local":
            return self._base_path
        return self._base_path / "users" / user_id

    async def _vault_for(self, user_id: str) -> Vault:
        existing = self._vaults.get(user_id)
        if existing is None:
            existing = Vault(await self._path_for(user_id))
            self._vaults[user_id] = existing
        self.vault = existing
        return existing

    async def load_config(self, user_id: str) -> None:
        """Load one user's vault path; users never share the default vault."""
        await self._vault_for(user_id)

    async def status(self, user_id: str) -> dict[str, Any]:
        vault = await self._vault_for(user_id)
        writable, error = await asyncio.to_thread(vault.check_writable)
        return {"path": str(vault.path), "writable": writable, "error": error}

    async def set_path(self, user_id: str, path: str) -> dict[str, Any]:
        resolved = await asyncio.to_thread(lambda: Path(path).expanduser().resolve())
        vault = Vault(resolved)
        self._vaults[user_id] = vault
        self.vault = vault
        async with self.sessions() as session:
            row = await session.scalar(select(UserSetting).where(UserSetting.user_id == user_id,
                                                                 UserSetting.key == CONFIG_KEY))
            value = {"path": str(self.vault.path)}
            if row is None:
                session.add(UserSetting(user_id=user_id, key=CONFIG_KEY, value=value))
            else:
                row.value = value
            await session.commit()
        return await self.status(user_id)

    async def read_run(self, user_id: str, run_id: str) -> RunRecord | None:
        vault = await self._vault_for(user_id)
        record = await asyncio.to_thread(vault.read_run, run_id)
        return record if record is not None and record.user_id == user_id else None

    async def list_runs(self, user_id: str) -> list[dict[str, Any]]:
        vault = await self._vault_for(user_id)
        records = await asyncio.to_thread(lambda: list(vault.iter_records()))

        def sort_key(record: RunRecord) -> tuple[str, str]:
            return record.created_at or "", record.run_id

        result: list[dict[str, Any]] = []
        for record in sorted(records, key=sort_key, reverse=True):
            if record.user_id != user_id:
                continue
            acted = [decision for decision in record.decisions if decision.acted]
            verdict = acted[-1].choice if acted else None
            note = vault.find_run_note(record.run_id)
            result.append({
                "run_id": record.run_id,
                "title": record.task.strip().splitlines()[0][:120] if record.task.strip() else "",
                "task": record.task,
                "mode": record.mode,
                "status": record.status,
                "created_at": record.created_at,
                "finished_at": record.finished_at,
                "verdict": verdict,
                "path": str(note.relative_to(vault.path)) if note is not None else None,
            })
        return result

    async def write_run(self, run_id: str) -> str | None:
        async with self.sessions() as session:
            record = await load_run_record(session, run_id)
        if record is None:
            return None
        vault = await self._vault_for(record.user_id)
        try:
            note = await asyncio.to_thread(vault.write_run, record)
        except (OSError, VaultError) as exc:
            self.events.publish("error", user_id=record.user_id, run_id=run_id,
                                data={"source": "vault", "error": str(exc)})
            return None
        relative = note.relative_to(vault.path).as_posix()
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.vault_note_path = relative
                await session.commit()
        self.events.publish("vault.written", user_id=record.user_id, run_id=run_id,
                            data={"path": relative})
        return relative

    async def delete_run(self, user_id: str, run_id: str) -> int:
        vault = await self._vault_for(user_id)
        return await asyncio.to_thread(vault.delete_run, run_id)

    async def reindex(self, user_id: str) -> dict[str, int]:
        vault = await self._vault_for(user_id)
        records = await asyncio.to_thread(lambda: list(vault.iter_records()))
        imported = 0
        skipped = 0
        async with self.sessions() as session:
            for record in records:
                if record.user_id != user_id:
                    continue
                if await session.get(Run, record.run_id) is not None:
                    skipped += 1
                    continue
                # A prior partial import may have left children behind after a
                # run row was removed. Rebuild those rows from the portable note.
                await session.execute(delete(AgentCall).where(AgentCall.run_id == record.run_id))
                await session.execute(delete(Decision).where(Decision.run_id == record.run_id))
                await session.execute(delete(Artifact).where(Artifact.run_id == record.run_id))
                note = vault.find_run_note(record.run_id)
                session.add(Run(
                    id=record.run_id,
                    user_id=record.user_id,
                    mode=record.mode,
                    task=record.task,
                    command=record.command,
                    agent_id=record.agent_id,
                    model_override=record.model_override,
                    status=record.status,
                    created_at=_datetime(record.created_at),
                    started_at=_datetime(record.started_at) if record.started_at else None,
                    finished_at=_datetime(record.finished_at) if record.finished_at else None,
                    final_output=record.final_output,
                    iteration_count=record.iteration_count,
                    total_tokens=record.total_tokens,
                    cost_estimate=record.cost_estimate,
                    vault_note_path=note.relative_to(vault.path).as_posix() if note is not None else None,
                     error=record.error,
                 ))
                await session.flush()
                pending = list(record.calls)
                known_call_ids = {call.call_id for call in pending}
                inserted_call_ids: set[str] = set()
                while pending:
                    call = next(
                        (
                            candidate
                            for candidate in pending
                            if not candidate.parent_call_id
                            or candidate.parent_call_id in inserted_call_ids
                            or candidate.parent_call_id not in known_call_ids
                        ),
                        pending[0],
                    )
                    parent_call_id = (
                        call.parent_call_id if call.parent_call_id in inserted_call_ids else None
                    )
                    session.add(AgentCall(
                        id=call.call_id, run_id=record.run_id, parent_call_id=parent_call_id,
                        agent_id=call.agent_id, agent_name=call.agent_name, role=call.role,
                        model_used=call.model_used, provider_id=call.provider_id, category=call.category,
                        depth=call.depth, status=call.status, input=call.input, output=call.output,
                        started_at=_datetime(call.started_at), ended_at=_datetime(call.ended_at)
                         if call.ended_at else None, tokens=call.tokens, cost=call.cost,
                         lineage=call.lineage, error=call.error,
                     ))
                    await session.flush()
                    inserted_call_ids.add(call.call_id)
                    pending.remove(call)
                for decision in record.decisions:
                    session.add(Decision(
                        id=decision.id, run_id=record.run_id, call_id=decision.call_id,
                        type=decision.type, engine=decision.engine, input_state_json=decision.input_state,
                        evidence_ref=decision.evidence_ref, choice=decision.choice,
                        confidence=decision.confidence, alternatives=decision.alternatives,
                        latency_ms=decision.latency_ms, fallback_used=decision.fallback_used,
                        shadow=decision.shadow, acted=decision.acted, reason=decision.reason,
                         created_at=_datetime(decision.created_at), label=decision.label,
                     ))
                artifacts = list(record.artifacts)
                if record.diff and not any(artifact.kind == "diff" for artifact in artifacts):
                    artifacts.append(ArtifactRecord(id=new_id("art_"), kind="diff", meta={"diff": record.diff}))
                for artifact in artifacts:
                    meta = dict(artifact.meta)
                    if artifact.kind == "diff":
                        meta["diff"] = record.diff
                    session.add(Artifact(id=artifact.id, run_id=record.run_id, call_id=artifact.call_id,
                                        kind=artifact.kind, path=artifact.path, meta=meta,
                                        created_at=_datetime(artifact.created_at)))
                imported += 1
            await session.commit()
        return {"imported": imported, "skipped": skipped, "total": imported + skipped}
