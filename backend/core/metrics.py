"""Prometheus metrics (one registry per app instance so tests can build many apps)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter("jevora_http_requests_total", "HTTP requests",
                                     ["method", "route", "status"], registry=self.registry)
        self.runs = Counter("jevora_runs_total", "Runs finished by mode and status",
                            ["mode", "status"], registry=self.registry)
        self.runs_active = Gauge("jevora_runs_active", "Runs currently executing", registry=self.registry)
        self.agents_running = Gauge("jevora_agents_running", "Top-level agents running", registry=self.registry)
        self.subagents_running = Gauge("jevora_subagents_running", "Subagents running", registry=self.registry)
        self.llm_calls = Counter("jevora_llm_calls_total", "Provider calls", ["provider_kind", "model", "status"],
                                 registry=self.registry)
        self.llm_tokens = Counter("jevora_llm_tokens_total", "Tokens used", ["provider_kind", "model", "type"],
                                  registry=self.registry)
        self.llm_cost = Counter("jevora_llm_cost_usd_total", "Estimated provider cost (USD)",
                                ["provider_kind", "model"], registry=self.registry)
        self.llm_latency = Histogram("jevora_llm_latency_seconds", "Provider call latency",
                                     ["provider_kind"], registry=self.registry)
        self.decisions = Counter("jevora_decisions_total", "Decision-layer decisions",
                                 ["type", "engine", "choice"], registry=self.registry)
        self.decision_latency = Histogram("jevora_decision_latency_seconds", "Decision latency",
                                          ["engine"], registry=self.registry,
                                          buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
        self.tool_calls = Counter("jevora_tool_calls_total", "Agent tool calls", ["tool", "status"],
                                  registry=self.registry)
        self.events = Counter("jevora_events_total", "Events published", ["type"], registry=self.registry)

    def render(self) -> bytes:
        return generate_latest(self.registry)
