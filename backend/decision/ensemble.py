"""Decision policy (EnsembleEngine) + decision logging + runtime-editable config.

Policy, per decision:
1. The RulesEngine always runs (deterministic safety net, cold-start engine).
2. The primary (Laya) runs when enabled. It may *act* only if its schema is ``validated``, its
   confidence clears its per-schema threshold, and the decision is not high-risk. Otherwise its
   verdict is logged in **shadow** mode and not acted on.
3. Otherwise the fallback chain decides: Jev (if configured and confident) → Rules.
4. Verification safety: if the acting engine is not Rules and it disagrees with Rules about
   ``confirm_done`` → escalate. A model can never confirm work that the evidence rules reject.
5. If the acting confidence is below the user-escalation threshold → escalate (for schemas that
   have an ``escalate`` option); routing falls back to the rules/default category instead.
Every engine verdict (acted or shadow) is logged with the state it saw, for evaluation/fine-tuning.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import get_logger
from backend.core.metrics import Metrics
from backend.db.models import Decision, SettingKV, UserSetting
from backend.decision.interface import DecisionEngine, EngineUnavailable
from backend.decision.jev_engine import JevEngine
from backend.decision.laya_engine import LayaEngine
from backend.decision.rules_engine import RulesEngine, distribution
from backend.decision.schemas import (
    ASSIGNMENT_CHOICES,
    COMPLETION_CHOICES,
    NEXT_ACTION_CHOICES,
    RETRY_CHOICES,
    ROUTING_CHOICES,
    SCHEMA_CHOICES,
    VERIFICATION_CHOICES,
    DecisionQuestion,
    DecisionResult,
    Evidence,
)

if TYPE_CHECKING:
    from backend.orchestrator.recorder import RunRecorder

log = get_logger(__name__)
CONFIG_KEY = "decision_config"
SCHEMAS = tuple(SCHEMA_CHOICES)


def _per_schema(value: float) -> dict[str, float]:
    return dict.fromkeys(SCHEMAS, value)


class DecisionConfig(BaseModel):
    escalation_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    thresholds: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "laya": _per_schema(0.80), "jev": _per_schema(0.70), "rules": _per_schema(0.0)})
    validated: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys(SCHEMAS, False))
    shadow_enabled: bool = True
    escalate_on_disagreement: bool = True
    primary: str = "laya"
    fallbacks: list[str] = Field(default_factory=lambda: ["jev", "rules"])

    def threshold(self, engine: str, schema: str) -> float:
        return self.thresholds.get(engine, {}).get(schema, 0.0)


class DecisionOutcome(BaseModel):
    result: DecisionResult
    decision_id: str | None = None
    shadow: list[DecisionResult] = Field(default_factory=list)
    rules: DecisionResult | None = None


class DecisionService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, laya: LayaEngine, jev: JevEngine,
                 rules: RulesEngine | None = None, metrics: Metrics | None = None,
                 default_threshold: float = 0.65) -> None:
        self.sessions = sessions
        self.metrics = metrics
        self.rules = rules or RulesEngine()
        self.laya = laya
        self.jev = jev
        self.config = DecisionConfig(escalation_threshold=default_threshold)
        self._configs: dict[str, DecisionConfig] = {}
        # Observers (e.g. plugin ``on_decision`` hooks) receive (result, user_id, run_id) after logging.
        self.observers: list[Callable[[DecisionResult, str | None, str | None], Awaitable[None]]] = []

    # ------------------------------------------------------------------ config
    async def load_config(self, user_id: str | None = None) -> DecisionConfig:
        async with self.sessions() as session:
            if user_id is None:
                row = await session.get(SettingKV, CONFIG_KEY)
            else:
                row = await session.scalar(select(UserSetting).where(UserSetting.user_id == user_id,
                                                                      UserSetting.key == CONFIG_KEY))
        base = self.config if user_id is None else DecisionConfig(escalation_threshold=self.config.escalation_threshold)
        if row is not None and isinstance(row.value, dict):
            merged = base.model_dump()
            merged.update(row.value)
            base = DecisionConfig.model_validate(merged)
        if user_id is None:
            self.config = base
        else:
            self._configs[user_id] = base
        return base

    async def get_config(self, user_id: str) -> DecisionConfig:
        if user_id not in self._configs:
            await self.load_config(user_id)
        return self._configs[user_id]

    async def save_config(self, updates: dict[str, Any], user_id: str | None = None) -> DecisionConfig:
        current = self.config if user_id is None else await self.get_config(user_id)
        merged = current.model_dump()
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                inner = dict(merged[key])
                for k, v in value.items():
                    inner[k] = {**inner.get(k, {}), **v} if isinstance(v, dict) and isinstance(inner.get(k), dict) \
                        else v
                merged[key] = inner
            else:
                merged[key] = value
        config = DecisionConfig.model_validate(merged)
        if user_id is None:
            self.config = config
        else:
            self._configs[user_id] = config
        async with self.sessions() as session:
            if user_id is None:
                row = await session.get(SettingKV, CONFIG_KEY)
            else:
                row = await session.scalar(select(UserSetting).where(UserSetting.user_id == user_id,
                                                                      UserSetting.key == CONFIG_KEY))
            if row is None and user_id is None:
                session.add(SettingKV(key=CONFIG_KEY, value=config.model_dump()))
            elif row is None:
                session.add(UserSetting(user_id=user_id, key=CONFIG_KEY, value=config.model_dump()))
            else:
                row.value = config.model_dump()
            await session.commit()
        return config

    def engine(self, name: str) -> DecisionEngine | None:
        return {"laya": self.laya, "jev": self.jev, "rules": self.rules}.get(name)

    async def active_engine(self, user_id: str | None = None) -> str:
        """The engine that currently acts on verification decisions (shown in the UI)."""
        cfg = self.config if user_id is None else await self.get_config(user_id)
        if await self.laya.available() and cfg.validated.get("verification"):
            return "Laya"
        if await self.jev.available():
            return "Jev"
        return "Rules"

    async def status(self, user_id: str | None = None) -> dict[str, Any]:
        cfg = self.config if user_id is None else await self.get_config(user_id)
        laya_ok = await self.laya.available()
        return {
            "ok": True,
            "active": await self.active_engine(user_id),
            "primary": {**self.laya.describe(), "available": laya_ok,
                        "validated": cfg.validated, "mode": "acting" if any(cfg.validated.values())
                        else "shadow"},
            "fallback": {**self.jev.describe(), "available": await self.jev.available()},
            "rules": {**self.rules.describe(), "available": True},
            "escalation_threshold": cfg.escalation_threshold,
        }

    # ------------------------------------------------------------------ core policy
    async def _run_engine(self, engine: DecisionEngine, question: DecisionQuestion,
                          evidence: Evidence | None) -> DecisionResult | None:
        try:
            if not await engine.available():
                return None
            return await engine.decide(question, evidence)
        except EngineUnavailable as exc:
            log.info("%s unavailable: %s", engine.name, exc)
            return None
        except Exception:
            log.exception("%s decision failed", engine.name)
            return None

    async def decide(self, question: DecisionQuestion, evidence: Evidence | None = None, *,
                     recorder: RunRecorder | None = None, call_id: str | None = None) -> DecisionOutcome:
        started = time.perf_counter()
        cfg = self.config if recorder is None else await self.get_config(recorder.user_id)
        schema = question.type
        rules_result = await self.rules.decide(question, evidence)
        shadow: list[DecisionResult] = []
        acting: DecisionResult | None = None
        fallback_used = False

        primary = self.engine(cfg.primary)
        if primary is not None and primary is not self.rules:
            primary_result = await self._run_engine(primary, question, evidence)
            if primary_result is not None:
                trusted = (cfg.validated.get(schema, False) and not question.high_risk
                           and primary_result.confidence >= cfg.threshold(primary.name, schema))
                if trusted:
                    acting = primary_result
                elif cfg.shadow_enabled:
                    primary_result.shadow = True
                    primary_result.reason = (primary_result.reason + "; shadow: " + (
                        "high-risk action" if question.high_risk else
                        "schema not validated" if not cfg.validated.get(schema, False) else "below threshold"
                    )).strip("; ")
                    shadow.append(primary_result)
        if acting is None:
            fallback_used = primary is not None and primary is not self.rules
            for name in cfg.fallbacks:
                engine = self.engine(name)
                if engine is None:
                    continue
                if engine is self.rules:
                    acting = rules_result
                    break
                result = await self._run_engine(engine, question, evidence)
                if result is not None and result.confidence >= cfg.threshold(engine.name, schema):
                    acting = result
                    break
                if result is not None:
                    result.shadow = True
                    result.reason += "; below threshold"
                    shadow.append(result)
            if acting is None:
                acting = rules_result

        acting_result = acting.model_copy()
        acting_result.fallback_used = fallback_used
        options = SCHEMA_CHOICES[schema]

        # 4. Safety: a model can never confirm work the evidence rules reject (and vice versa).
        if (schema == "verification" and acting_result.engine != "rules" and cfg.escalate_on_disagreement
                and (acting_result.choice == "confirm_done") != (rules_result.choice == "confirm_done")):
            acting_result = DecisionResult(type=schema, choice="escalate", confidence=round(1 - abs(
                acting_result.confidence - rules_result.confidence) / 2, 4),
                scores_per_option=distribution(list(options), "escalate", 0.6), engine=f"{acting_result.engine}+rules",
                fallback_used=fallback_used,
                reason=(f"engines disagree: {acting_result.engine}={acting_result.choice} "
                        f"vs rules={rules_result.choice}"))

        # 5. Low confidence → user escalation (or default category for routing).
        if acting_result.confidence < cfg.escalation_threshold:
            if "escalate" in options and acting_result.choice != "escalate":
                acting_result = DecisionResult(
                    type=schema, choice="escalate", confidence=round(1 - acting_result.confidence, 4),
                                        scores_per_option=acting_result.scores_per_option, engine=acting_result.engine,
                                        fallback_used=fallback_used,
                                        reason=f"confidence {acting_result.confidence:.2f} below escalation threshold "
                                               f"{cfg.escalation_threshold:.2f} ({acting_result.reason})")
            elif schema == "routing" and acting_result.engine != "rules":
                acting_result = rules_result.model_copy(update={"fallback_used": True})
        acting_result.shadow_results = shadow
        if not acting_result.latency_ms:
            acting_result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        decision_id = await self._log(question, evidence, acting_result, shadow, recorder, call_id)
        for observer in self.observers:
            try:
                await observer(
                    acting_result, recorder.user_id if recorder else None, recorder.run_id if recorder else None
                )
            except Exception:
                log.exception("decision observer failed")
        return DecisionOutcome(result=acting_result, decision_id=decision_id, shadow=shadow, rules=rules_result)

    async def _log(self, question: DecisionQuestion, evidence: Evidence | None, acting: DecisionResult,
                   shadow: list[DecisionResult], recorder: RunRecorder | None, call_id: str | None) -> str | None:
        state = {"question": question.model_dump(), "evidence": [i.model_dump() for i in (evidence.items if evidence
                                                                                          else [])]}
        refs = evidence.refs() if evidence else []
        decision_id: str | None = None
        for result in [*shadow, acting]:
            acted = result is acting
            if recorder is not None:
                logged = await recorder.record_decision(
                    type=question.type, engine=result.engine, choice=result.choice, confidence=result.confidence,
                    alternatives=result.scores_per_option, latency_ms=result.latency_ms, input_state=state,
                    evidence_ref=refs, call_id=call_id, fallback_used=result.fallback_used,
                    shadow=result.shadow, acted=acted, reason=result.reason)
            else:
                async with self.sessions() as session:
                    row = Decision(type=question.type, engine=result.engine, choice=result.choice,
                                   confidence=result.confidence, alternatives=result.scores_per_option,
                                   latency_ms=result.latency_ms, input_state_json=state, evidence_ref=refs,
                                   fallback_used=result.fallback_used, shadow=result.shadow, acted=acted,
                                   reason=result.reason, call_id=call_id)
                    session.add(row)
                    await session.commit()
                    logged = row.id
                if self.metrics is not None:
                    self.metrics.decisions.labels(question.type, result.engine, result.choice).inc()
            if acted:
                decision_id = logged
        return decision_id

    # ------------------------------------------------------------------ typed operations
    async def verify_work(self, state: dict[str, Any], evidence: Evidence, **kw: Any) -> DecisionOutcome:
        if not isinstance(evidence, Evidence):
            raise TypeError("verify_work requires Evidence (tool output), never agent narrative")
        return await self.decide(DecisionQuestion(type="verification", options=list(VERIFICATION_CHOICES),
                                                  state=state, high_risk=bool(state.get("high_risk"))),
                                 evidence, **kw)

    async def route_model(self, task_spec: dict[str, Any], **kw: Any) -> DecisionOutcome:
        return await self.decide(DecisionQuestion(type="routing", options=list(ROUTING_CHOICES), state=task_spec),
                                 None, **kw)

    async def assign_agent(self, state: dict[str, Any], **kw: Any) -> DecisionOutcome:
        return await self.decide(DecisionQuestion(type="assignment", options=list(ASSIGNMENT_CHOICES), state=state),
                                 None, **kw)

    async def select_next_action(self, state: dict[str, Any], **kw: Any) -> DecisionOutcome:
        return await self.decide(DecisionQuestion(type="next_action", options=list(NEXT_ACTION_CHOICES),
                                                  state=state), None, **kw)

    async def decide_retry(self, state: dict[str, Any], **kw: Any) -> DecisionOutcome:
        return await self.decide(DecisionQuestion(type="retry", options=list(RETRY_CHOICES), state=state), None, **kw)

    async def confirm_completion(self, state: dict[str, Any], evidence: Evidence, **kw: Any) -> DecisionOutcome:
        return await self.decide(DecisionQuestion(type="completion", options=list(COMPLETION_CHOICES), state=state),
                                 evidence, **kw)
