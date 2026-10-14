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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import get_logger
from backend.core.metrics import Metrics
from backend.db.models import Decision, SettingKV
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
        laya._validated = lambda schema: bool(self.config.validated.get(schema, False))
        # Observers (e.g. plugin ``on_decision`` hooks) receive (result, user_id, run_id) after logging.
        self.observers: list[Callable[[DecisionResult, str | None, str | None], Awaitable[None]]] = []

    # ------------------------------------------------------------------ config
    async def load_config(self) -> DecisionConfig:
        async with self.sessions() as session:
            row = await session.get(SettingKV, CONFIG_KEY)
        if row is not None and isinstance(row.value, dict):
            merged = self.config.model_dump()
            merged.update(row.value)
            self.config = DecisionConfig.model_validate(merged)
        return self.config

    async def save_config(self, updates: dict[str, Any]) -> DecisionConfig:
        merged = self.config.model_dump()
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                inner = dict(merged[key])
                for k, v in value.items():
                    inner[k] = {**inner.get(k, {}), **v} if isinstance(v, dict) and isinstance(inner.get(k), dict) \
                        else v
                merged[key] = inner
            else:
                merged[key] = value
        self.config = DecisionConfig.model_validate(merged)
        async with self.sessions() as session:
            row = await session.get(SettingKV, CONFIG_KEY)
            if row is None:
                session.add(SettingKV(key=CONFIG_KEY, value=self.config.model_dump()))
            else:
                row.value = self.config.model_dump()
            await session.commit()
        return self.config

    def engine(self, name: str) -> DecisionEngine | None:
        return {"laya": self.laya, "jev": self.jev, "rules": self.rules}.get(name)

    async def active_engine(self) -> str:
        """The engine that currently acts on verification decisions (shown in the UI)."""
        if await self.laya.available() and self.config.validated.get("verification"):
            return "Laya"
        if await self.jev.available():
            return "Jev"
        return "Rules"

    async def status(self) -> dict[str, Any]:
        laya_ok = await self.laya.available()
        return {
            "ok": True,
            "active": await self.active_engine(),
            "primary": {**self.laya.describe(), "available": laya_ok,
                        "validated": self.config.validated, "mode": "acting" if any(self.config.validated.values())
                        else "shadow"},
            "fallback": {**self.jev.describe(), "available": await self.jev.available()},
            "rules": {**self.rules.describe(), "available": True},
            "escalation_threshold": self.config.escalation_threshold,
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
        cfg = self.config
        schema = question.type
        rules_result = await self.rules.decide(question, evidence)
        shadow: list[DecisionResult] = []
        acting: DecisionResult | None = None
        fallback_used = False

        primary = self.engine(cfg.primary)
        if primary is not None and primary is not self.rules:
            primary_result = await self._run_engine(primary, question, evidence)
            if primary_result is not None:
                trusted = (primary.validated(schema) and not question.high_risk
                           and primary_result.confidence >= cfg.threshold(primary.name, schema))
                if trusted:
                    acting = primary_result
                elif cfg.shadow_enabled:
                    primary_result.shadow = True
                    primary_result.reason = (primary_result.reason + "; shadow: " + (
                        "high-risk action" if question.high_risk else
                        "schema not validated" if not primary.validated(schema) else "below threshold")).strip("; ")
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

        acting = acting.model_copy()
        acting.fallback_used = fallback_used
        options = SCHEMA_CHOICES[schema]

        # 4. Safety: a model can never confirm work the evidence rules reject (and vice versa).
        if (schema == "verification" and acting.engine != "rules" and cfg.escalate_on_disagreement
                and (acting.choice == "confirm_done") != (rules_result.choice == "confirm_done")):
            acting = DecisionResult(type=schema, choice="escalate", confidence=round(1 - abs(
                acting.confidence - rules_result.confidence) / 2, 4),
                scores_per_option=distribution(list(options), "escalate", 0.6), engine=f"{acting.engine}+rules",
                fallback_used=fallback_used,
                reason=f"engines disagree: {acting.engine}={acting.choice} vs rules={rules_result.choice}")

        # 5. Low confidence → user escalation (or default category for routing).
        if acting.confidence < cfg.escalation_threshold:
            if "escalate" in options and acting.choice != "escalate":
                acting = DecisionResult(type=schema, choice="escalate", confidence=round(1 - acting.confidence, 4),
                                        scores_per_option=acting.scores_per_option, engine=acting.engine,
                                        fallback_used=fallback_used,
                                        reason=f"confidence {acting.confidence:.2f} below escalation threshold "
                                               f"{cfg.escalation_threshold:.2f} ({acting.reason})")
            elif schema == "routing" and acting.engine != "rules":
                acting = rules_result.model_copy(update={"fallback_used": True})
        acting.shadow_results = shadow
        if not acting.latency_ms:
            acting.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        decision_id = await self._log(question, evidence, acting, shadow, recorder, call_id)
        for observer in self.observers:
            try:
                await observer(acting, recorder.user_id if recorder else None, recorder.run_id if recorder else None)
            except Exception:
                log.exception("decision observer failed")
        return DecisionOutcome(result=acting, decision_id=decision_id, shadow=shadow, rules=rules_result)

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
