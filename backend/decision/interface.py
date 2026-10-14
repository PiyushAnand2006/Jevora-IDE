"""The model-agnostic Decision Engine interface (PRD §3.2 / architecture §2.3).

The orchestrator depends only on this interface. Engines are non-generative: they return a
typed choice with a confidence and per-option scores — never code or prose.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from backend.decision.schemas import (
    ASSIGNMENT_CHOICES,
    COMPLETION_CHOICES,
    NEXT_ACTION_CHOICES,
    RETRY_CHOICES,
    ROUTING_CHOICES,
    VERIFICATION_CHOICES,
    DecisionQuestion,
    DecisionResult,
    Evidence,
)


class EngineUnavailable(Exception):
    pass


class DecisionEngine(ABC):
    name: str = "abstract"

    async def available(self) -> bool:
        return True

    def validated(self, decision_type: str) -> bool:
        """Whether this engine is trusted to act (not just shadow) for a schema."""
        return True

    @abstractmethod
    async def _decide(self, question: DecisionQuestion, evidence: Evidence | None) -> DecisionResult: ...

    async def decide(self, question: DecisionQuestion, evidence: Evidence | None = None) -> DecisionResult:
        started = time.perf_counter()
        result = await self._decide(question, evidence)
        if not result.latency_ms:
            result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    # ---- the six typed operations ------------------------------------------------------
    async def verify_work(self, state: dict[str, Any], evidence: Evidence) -> DecisionResult:
        if not isinstance(evidence, Evidence):
            raise TypeError("verify_work requires an Evidence object (tool output), never agent narrative")
        return await self.decide(DecisionQuestion(type="verification", options=list(VERIFICATION_CHOICES),
                                                  state=state, high_risk=bool(state.get("high_risk"))), evidence)

    async def select_next_action(self, state: dict[str, Any]) -> DecisionResult:
        return await self.decide(DecisionQuestion(type="next_action", options=list(NEXT_ACTION_CHOICES), state=state))

    async def assign_agent(self, state: dict[str, Any]) -> DecisionResult:
        return await self.decide(DecisionQuestion(type="assignment", options=list(ASSIGNMENT_CHOICES), state=state))

    async def decide_retry(self, state: dict[str, Any]) -> DecisionResult:
        return await self.decide(DecisionQuestion(type="retry", options=list(RETRY_CHOICES), state=state))

    async def confirm_completion(self, state: dict[str, Any], evidence: Evidence) -> DecisionResult:
        if not isinstance(evidence, Evidence):
            raise TypeError("confirm_completion requires an Evidence object")
        return await self.decide(DecisionQuestion(type="completion", options=list(COMPLETION_CHOICES),
                                                  state=state), evidence)

    async def route_model(self, task_spec: dict[str, Any]) -> DecisionResult:
        return await self.decide(DecisionQuestion(type="routing", options=list(ROUTING_CHOICES), state=task_spec))

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}
