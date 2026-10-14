"""LangGraph shared state (the run's scratchpad). Everything here is JSON-serialisable for checkpoints."""

from __future__ import annotations

from typing import Any, TypedDict

from backend.decision.schemas import Evidence, EvidenceItem


class GraphState(TypedDict, total=False):
    run_id: str
    user_id: str
    mode: str
    task: str
    command: str | None
    command_arg: str
    agent_id: str | None
    model_override: str | None
    conversation_id: str | None
    attachments: list[str]
    flags: dict[str, bool]
    # routing
    spec: dict[str, Any]
    category: str
    assignee: str
    worker_node: str
    # build loop
    plan: str
    iteration: int
    max_iterations: int
    same_agent_failures: int
    worker_attempts: int
    stronger: bool
    feedback: str
    evidence: list[dict[str, Any]]
    files_changed: list[str]
    worker_output: str
    worker_status: str
    worker_error: str | None
    worker_call_id: str | None
    tests_written: bool
    review: str
    reviewed: bool
    requires_tests: bool
    requires_changes: bool
    # verification / escalation
    verification: dict[str, Any]
    escalation: dict[str, Any] | None
    # research / analysis
    sources: list[dict[str, Any]]
    web_note: str | None
    analysis: dict[str, Any]
    report_path: str | None
    # outcome
    final_status: str
    final_output: str
    error: str | None


def evidence_of(state: GraphState) -> Evidence:
    """Rebuild Evidence from checkpointed dicts. Signatures are re-verified on construction."""
    return Evidence(items=[EvidenceItem.model_validate(item) for item in state.get("evidence", [])])


def dump_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]
