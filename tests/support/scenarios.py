"""Scripted multi-agent scenarios for orchestrator tests."""

from __future__ import annotations

import json
from typing import Any

from tests.support.brain import Brain, Turn, call, tools_reply
from tests.support.servers import MockReply

CALC_OK = "def add(a, b):\n    return a + b\n"
CALC_BUG = "def add(a, b):\n    return a - b\n"
TEST_CALC = ("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
             "def test_zero():\n    assert add(0, 0) == 0\n")

TRIAGE = {"title": "calculator", "intent": "build", "needs_code_changes": True, "needs_tests": True,
          "ui_work": False, "keywords": ["python", "add"], "risk": "low"}


def router_rule(spec: dict[str, Any] | None = None) -> Any:
    def rule(turn: Turn) -> MockReply | None:
        if turn.role == "router":
            return MockReply(content=json.dumps(spec or TRIAGE))
        return None

    return rule


def planner_rule(turn: Turn) -> MockReply | None:
    if turn.role == "planner":
        return MockReply(content="1. Write calc.py with add(a, b).\n2. Write test_calc.py.\n"
                                 "Verify with: python -m pytest -q")
    return None


def critic_rule(turn: Turn) -> MockReply | None:
    if turn.role == "critic":
        return MockReply(content="No defects found. Tests cover the behaviour.")
    return None


class CoderScript:
    """Coder that writes buggy code for the first ``buggy`` attempts, always claiming success."""

    def __init__(self, buggy: int = 0, delay: float = 0.0) -> None:
        self.buggy = buggy
        self.delay = delay
        self.attempts = 0

    def __call__(self, turn: Turn) -> MockReply | None:
        if turn.role != "coder":
            return None
        if turn.step == 0:
            self.attempts += 1
            code = CALC_BUG if self.attempts <= self.buggy else CALC_OK
            return MockReply(tool_calls=[
                {"id": "w1", "name": "write_file", "arguments": {"path": "calc.py", "content": code}},
                {"id": "w2", "name": "write_file", "arguments": {"path": "test_calc.py", "content": TEST_CALC}},
            ], delay=self.delay)
        if turn.step == 1:
            return tools_reply(call("run_tests"))
        return MockReply(content="All tests pass! The calculator is implemented and verified.")


def build_brain(buggy: int = 0, delay: float = 0.0, spec: dict[str, Any] | None = None) -> tuple[Brain, CoderScript]:
    coder = CoderScript(buggy, delay)
    brain = Brain().on(router_rule(spec)).on(planner_rule).on(coder).on(critic_rule)
    return brain, coder
