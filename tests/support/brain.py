"""A scripted "brain" for the mock LLM: picks replies by agent role, task and step."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from tests.support.servers import MockLLM, MockReply

ROLE_MARKERS = {
    "You are the planning agent": "planner",
    "You are the coding agent": "coder",
    "You are the testing agent": "tester",
    "You are the code-review agent": "critic",
    "You are the UI design agent": "ui-designer",
    "You are the research agent": "researcher",
    "You are the analysis agent": "analyst",
    "You are the screen-control testing agent": "screen-tester",
    "You are Jevora, a helpful engineering assistant": "chat",
    "You design autonomous coding agents": "generator",
    "You triage tasks for an agentic IDE": "router",
}


def call(tool_name: str, /, **arguments: Any) -> dict[str, Any]:
    return {"name": tool_name, "arguments": arguments}


class Turn:
    """What the brain sees for one request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.messages: list[dict[str, Any]] = body.get("messages", [])
        system = next((m.get("content") for m in self.messages if m.get("role") == "system"), "") or ""
        self.system = system if isinstance(system, str) else json.dumps(system)
        self.role = next((r for marker, r in ROLE_MARKERS.items() if marker in self.system), "unknown")
        users = [m for m in self.messages if m.get("role") == "user"]
        first = users[0].get("content") if users else ""
        self.task = first if isinstance(first, str) else json.dumps(first)
        self.step = sum(1 for m in self.messages if m.get("role") == "assistant")
        self.model = body.get("model")
        self.has_tools = bool(body.get("tools"))
        self.json_schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema")

    def tool_results(self) -> list[str]:
        return [str(m.get("content")) for m in self.messages if m.get("role") == "tool"]

    def last_tool_result(self) -> str:
        results = self.tool_results()
        return results[-1] if results else ""

    def mentions(self, pattern: str) -> bool:
        return re.search(pattern, self.task, re.I) is not None


Rule = Callable[[Turn], MockReply | None]


class Brain:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self.turns: list[Turn] = []

    def on(self, rule: Rule) -> Brain:
        self.rules.append(rule)
        return self

    def __call__(self, body: dict[str, Any]) -> MockReply:
        turn = Turn(body)
        self.turns.append(turn)
        for rule in self.rules:
            reply = rule(turn)
            if reply is not None:
                return reply
        return MockReply(content=f"[{turn.role}] done.")

    def install(self, llm: MockLLM) -> Brain:
        llm.handler = self
        return self

    def roles(self) -> list[str]:
        return [t.role for t in self.turns]


def tools_reply(*calls: dict[str, Any]) -> MockReply:
    return MockReply(tool_calls=[{"id": f"call_{i}_{c['name']}", **c} for i, c in enumerate(calls)])
