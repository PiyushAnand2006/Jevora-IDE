from __future__ import annotations

import pytest

from backend.decision_engine import HeuristicDecisionEngine, get_decision_engine
from backend.schemas import Evidence, TaskCategory, WorkVerdict


@pytest.mark.parametrize("task,mode,expected", [
    ("Review this architecture", "analysis", TaskCategory.heavy_reasoning),
    ("Classify these documents", "build", TaskCategory.fast_classification),
    ("Implement the endpoint", "build", TaskCategory.code_generation),
    ("Design a CSS dashboard", "build", TaskCategory.creative_ui),
    ("Research competing libraries", "research", TaskCategory.research_retrieval),
])
def test_heuristic_engine_returns_valid_routing_decisions(task, mode, expected):
    decision = HeuristicDecisionEngine().route(task, mode)

    assert decision.choice == expected.value
    assert 0 <= decision.confidence <= 1
    assert decision.kind == "model_routing"


@pytest.mark.parametrize("evidence", [
    Evidence(test_passed=True, tests_run=1),
    Evidence(test_passed=False, tests_run=1, exec_log_tail="failed"),
    Evidence(files_changed=[]),
])
def test_heuristic_engine_returns_valid_verdict(evidence):
    decision = HeuristicDecisionEngine().verify(evidence)

    assert decision.choice in {item.value for item in WorkVerdict}
    assert 0 <= decision.confidence <= 1
    assert decision.kind == "work_verification"


def test_default_engine_is_the_offline_heuristic_implementation():
    assert isinstance(get_decision_engine(), HeuristicDecisionEngine)
