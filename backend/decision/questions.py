"""Shared decision-question prompts and state serialisation for the Jev engine.

These describe every decision schema (per-option criteria and instructions) and
the compact, deterministic state/evidence serialisation the hosted Jev API
consumes — never agent narrative, only machine-produced features.
"""

from __future__ import annotations

import json
from typing import Any

from backend.decision.schemas import DecisionQuestion, Evidence

OPTION_CRITERIA: dict[str, dict[str, str]] = {
    "verification": {
        "confirm_done": "the evidence shows the task is complete: tests pass and the required changes exist",
        "restart_same_agent": "the evidence shows failures the same agent can fix by trying again",
        "reassign_new_agent": "the same agent keeps failing; a different agent or stronger model should take over",
        "escalate": "the evidence is contradictory, risky or the retry budget is exhausted; ask the user",
    },
    "routing": {
        "heavy_reasoning": "complex planning, architecture, analysis or multi-step reasoning",
        "fast_classification": "short, simple classification or conversational reply",
        "code_generation": "writing or fixing source code and tests",
        "creative_ui": "visual design, UI layout, styling or front-end components",
        "research_retrieval": "finding, comparing and summarising information from sources",
    },
    "next_action": {o: o.replace("_", " ") for o in ("plan", "code", "design_ui", "research", "analyze", "test",
                                                     "critique", "verify", "finalize", "escalate")},
    "assignment": {o: f"the {o} agent" for o in ("planner", "coder", "tester", "critic", "ui-designer",
                                                  "researcher", "analyst", "screen-tester", "chat")},
    "retry": {"retry_same": "transient error; retry the same way", "retry_stronger_model": "use a stronger model",
              "give_up": "the error cannot be fixed by retrying", "escalate": "ask the user"},
    "completion": {"complete": "the output fully answers the request with grounded evidence",
                   "incomplete": "the output is missing parts or is not grounded",
                   "escalate": "unclear; ask the user"},
}
INSTRUCTIONS: dict[str, str] = {
    "verification": "Given only the machine-produced evidence, is the agent's work done?",
    "routing": "Which model category fits this task?",
    "next_action": "What should the orchestrator do next?",
    "assignment": "Which agent should handle this task?",
    "retry": "How should the system react to this failure?",
    "completion": "Is the output complete and grounded?",
}


def state_text(question: DecisionQuestion, evidence: Evidence | None) -> str:
    """Compact, deterministic serialisation of features + evidence (never agent narrative)."""
    payload: dict[str, Any] = {"state": question.state}
    if evidence is not None and evidence.items:
        payload["evidence"] = [{"kind": i.kind, **{k: v for k, v in i.payload.items()
                                                   if k not in ("output_tail", "diff_tail", "stdout_tail",
                                                                "stderr_tail", "action_log")}}
                               for i in evidence.items[-8:]]
    return json.dumps(payload, sort_keys=True, default=str)[:6000]


def decision_questions(question: DecisionQuestion) -> dict[str, Any]:
    criteria = OPTION_CRITERIA[question.type]
    return {"decision": {"type": "choice", "instructions": question.instructions or INSTRUCTIONS[question.type],
                         "criteria": {o: criteria.get(o, o) for o in question.options}}}
