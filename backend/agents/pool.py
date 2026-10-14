"""Agent pool: live call tree per run, cancellation propagation, run budgets, running counters."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from backend.agents.config import AgentConfig
from backend.core.errors import BudgetExceeded
from backend.core.events import EventBus
from backend.core.metrics import Metrics


def task_key(config: AgentConfig, task: str) -> str:
    normalized = " ".join(task.lower().split())[:2000]
    digest = hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"{config.template or config.name}:{digest}"


@dataclass
class CallNode:
    call_id: str
    config: AgentConfig
    key: str
    depth: int
    parent: CallNode | None = None
    children: list[CallNode] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False
    user_id: str = ""
    spawned: int = 0  # fan-out slots reserved synchronously (parallel spawns cannot race past the limit)

    def ancestors(self) -> list[CallNode]:
        out: list[CallNode] = []
        node = self.parent
        while node is not None:
            out.append(node)
            node = node.parent
        return out

    def lineage(self) -> list[str]:
        return [n.call_id for n in reversed(self.ancestors())]

    def descendants(self) -> list[CallNode]:
        out: list[CallNode] = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        return out

    def cancel(self) -> None:
        """Cancel this call and every descendant (children first)."""
        self.cancelled = True
        for child in self.children:
            child.cancel()
        if self.task is not None and not self.task.done():
            self.task.cancel()


@dataclass
class RunBudget:
    max_tokens: int
    max_cost_usd: float
    max_seconds: float
    tokens: int = 0
    cost: float = 0.0
    started: float = field(default_factory=time.monotonic)

    def add(self, tokens: int, cost: float) -> None:
        self.tokens += tokens
        self.cost += cost

    def check(self) -> None:
        if self.tokens > self.max_tokens:
            raise BudgetExceeded(f"run token budget exceeded ({self.tokens} > {self.max_tokens})",
                                 code="token_budget")
        if self.cost > self.max_cost_usd:
            raise BudgetExceeded(f"run cost budget exceeded (${self.cost:.4f} > ${self.max_cost_usd:.2f})",
                                 code="cost_budget")
        elapsed = time.monotonic() - self.started
        if elapsed > self.max_seconds:
            raise BudgetExceeded(f"run time budget exceeded ({elapsed:.0f}s > {self.max_seconds:.0f}s)",
                                 code="time_budget")


@dataclass
class RunScope:
    run_id: str
    user_id: str
    budget: RunBudget
    nodes: dict[str, CallNode] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def roots(self) -> list[CallNode]:
        return [n for n in self.nodes.values() if n.parent is None]

    def cancel(self) -> int:
        self.cancelled = True
        count = 0
        for root in self.roots:
            count += 1 + len(root.descendants())
            root.cancel()
        return count


class AgentPool:
    def __init__(self, events: EventBus, metrics: Metrics | None = None) -> None:
        self.events = events
        self.metrics = metrics
        self._scopes: dict[str, RunScope] = {}
        self._running: dict[str, CallNode] = {}

    def scope(self, run_id: str, user_id: str, budget: RunBudget | None = None) -> RunScope:
        existing = self._scopes.get(run_id)
        if existing is None:
            existing = RunScope(run_id=run_id, user_id=user_id,
                                budget=budget or RunBudget(10**9, 10**6, 10**9))
            self._scopes[run_id] = existing
        return existing

    def get_scope(self, run_id: str) -> RunScope | None:
        return self._scopes.get(run_id)

    def drop(self, run_id: str) -> None:
        self._scopes.pop(run_id, None)

    def cancel_run(self, run_id: str) -> int:
        scope = self._scopes.get(run_id)
        return scope.cancel() if scope is not None else 0

    def enter(self, scope: RunScope, node: CallNode) -> None:
        scope.nodes[node.call_id] = node
        if node.parent is not None:
            node.parent.children.append(node)
        node.user_id = scope.user_id
        self._running[node.call_id] = node
        self._publish(scope.user_id)

    def exit(self, scope: RunScope, node: CallNode) -> None:
        self._running.pop(node.call_id, None)
        self._publish(scope.user_id)

    def status(self, user_id: str | None = None) -> dict[str, int]:
        nodes = [n for n in self._running.values() if user_id is None or n.user_id == user_id]
        return {"agents_running": sum(1 for n in nodes if n.depth == 0),
                "subagents_running": sum(1 for n in nodes if n.depth > 0)}

    def _publish(self, user_id: str) -> None:
        overall = self.status()
        if self.metrics is not None:
            self.metrics.agents_running.set(overall["agents_running"])
            self.metrics.subagents_running.set(overall["subagents_running"])
        self.events.publish("agents.status", user_id=user_id, data=self.status(user_id))
