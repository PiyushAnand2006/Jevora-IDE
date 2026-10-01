"""Model-agnostic, typed decision engine interface and safe offline default."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .config import ESCALATION_CONFIDENCE_THRESHOLD
from .schemas import Decision, Evidence, TaskCategory, WorkVerdict


class DecisionEngine(ABC):
    name: str

    @abstractmethod
    def route(self, task: str, mode: str) -> Decision: ...

    @abstractmethod
    def verify(self, evidence: Evidence) -> Decision: ...


class HeuristicDecisionEngine(DecisionEngine):
    """Deterministic baseline. It is explicit about uncertainty, not an LLM."""
    name = "heuristic"

    def route(self, task: str, mode: str) -> Decision:
        text = task.lower()
        if mode == "research" or any(w in text for w in ("research", "find", "compare", "summar")):
            category = TaskCategory.research_retrieval
        elif mode == "analysis" or any(w in text for w in ("analy", "review", "debug", "architecture")):
            category = TaskCategory.heavy_reasoning
        elif any(w in text for w in ("design", "css", "ui", "frontend", "layout")):
            category = TaskCategory.creative_ui
        elif any(w in text for w in ("classify", "tag", "extract")):
            category = TaskCategory.fast_classification
        else:
            category = TaskCategory.code_generation
        return Decision(kind="model_routing", choice=category.value, confidence=0.78,
                        engine=self.name, rationale="Fixed taxonomy classification.")

    def verify(self, evidence: Evidence) -> Decision:
        if evidence.test_passed is False or evidence.lint_ok is False:
            return Decision(kind="work_verification", choice=WorkVerdict.restart_same_agent.value,
                            confidence=0.92, engine=self.name, rationale="A tool check failed.")
        if evidence.tests_run > 0 and evidence.test_passed is True:
            return Decision(kind="work_verification", choice=WorkVerdict.confirm_done.value,
                            confidence=0.93, engine=self.name, rationale="Tests supplied positive evidence.")
        confidence = 0.48 if not evidence.files_changed else 0.62
        choice = WorkVerdict.escalate.value if confidence < ESCALATION_CONFIDENCE_THRESHOLD else WorkVerdict.confirm_done.value
        return Decision(kind="work_verification", choice=choice, confidence=confidence,
                        engine=self.name, rationale="No executable test evidence was available.",
                        escalate=choice == WorkVerdict.escalate.value)


def get_decision_engine() -> DecisionEngine:
    # Laya/Jev adapters deliberately share this interface. Until configured and evaluated,
    # the safe deterministic baseline prevents a zero-shot model from approving work.
    return HeuristicDecisionEngine()
