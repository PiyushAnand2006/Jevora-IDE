"""Decision datasets: JSONL items, synthetic seed data, and export of logged/labelled decisions."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.db.models import Decision
from backend.decision.schemas import (
    PAYLOAD_MODELS,
    SCHEMA_CHOICES,
    DecisionQuestion,
    Evidence,
    EvidenceFactory,
    EvidenceItem,
)


class EvidenceSpec(BaseModel):
    kind: str
    source: str = "sandbox"
    payload: dict[str, Any]


class DatasetItem(BaseModel):
    id: str
    type: str
    state: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceSpec] = Field(default_factory=list)
    label: str
    origin: str = "seed"

    def question(self) -> DecisionQuestion:
        return DecisionQuestion(type=self.type, options=list(SCHEMA_CHOICES[self.type]),
                                state=self.state, high_risk=bool(self.state.get("high_risk")))

    def evidence_obj(self) -> Evidence:
        """Eval data is trusted fixture data, so it is signed here exactly like tool output."""
        items = [EvidenceFactory.make(e.kind, e.source, PAYLOAD_MODELS[e.kind].model_validate(e.payload))
                 for e in self.evidence]
        return Evidence(items=items)


def load_jsonl(path: Path) -> list[DatasetItem]:
    items: list[DatasetItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(DatasetItem.model_validate_json(line))
    return items


def write_jsonl(items: list[DatasetItem], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(i.model_dump_json() for i in items) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- synthetic seed
def _tests(passed: int, failed: int = 0, errors: int = 0, exit_code: int | None = None, timed_out: bool = False,
           command: str = "python -m pytest -q") -> EvidenceSpec:
    code = exit_code if exit_code is not None else (0 if failed == errors == 0 else 1)
    return EvidenceSpec(kind="test_result", payload={
        "framework": "pytest", "command": command, "exit_code": code, "passed": passed, "failed": failed,
        "errors": errors, "collected": passed + failed + errors, "timed_out": timed_out})


def _diff(files: int, additions: int = 20, deletions: int = 2) -> EvidenceSpec:
    return EvidenceSpec(kind="diff", source="workspace_diff", payload={
        "files_changed": files, "additions": additions if files else 0, "deletions": deletions if files else 0,
        "files": [f"src/file_{i}.py" for i in range(files)]})


def _lint(errors: int) -> EvidenceSpec:
    return EvidenceSpec(kind="lint", payload={"tool": "ruff", "errors": errors, "exit_code": 1 if errors else 0})


def _screen(passed: int, failed: int, console_errors: int = 0) -> EvidenceSpec:
    assertions = [{"name": f"check_{i}", "passed": True} for i in range(passed)] + [
        {"name": f"fail_{i}", "passed": False, "detail": "element not visible"} for i in range(failed)]
    return EvidenceSpec(kind="screenshot", source="screen_driver", payload={
        "url": "http://localhost:5000", "screenshots": [{"path": "s1.png", "sha256": "0" * 64}],
        "assertions": assertions, "console_errors": [f"TypeError {i}" for i in range(console_errors)], "steps": 4})


ROUTING_SEED: dict[str, list[str]] = {
    "code_generation": ["Implement a REST endpoint that returns user profiles", "Fix the bug in parse_date()",
                        "Write a Python script that renames files", "Add pytest tests for the cart module",
                        "Create a Flask hello-world app with a test", "Refactor the database layer into a class",
                        "Build a CLI that converts CSV to JSON", "Write a function to merge two sorted lists"],
    "creative_ui": ["Design a landing page hero section with a gradient", "Make the navbar responsive",
                    "Restyle the settings form to match our dark theme", "Create a pricing page layout",
                    "Improve the CSS animations on the modal", "Design a dashboard UI with cards"],
    "research_retrieval": ["Research the differences between Postgres and MySQL", "Summarize the latest docs on OAuth",
                           "Compare three Python web frameworks with sources", "Find out what WebGPU supports",
                           "What is the history of the HTTP/3 protocol? cite sources"],
    "heavy_reasoning": ["Plan the architecture for a multi-tenant SaaS", "Analyze the performance bottleneck",
                        "Find the root cause of this race condition", "Design an algorithm for scheduling",
                        "Evaluate the security trade-offs of our auth strategy"],
    "fast_classification": ["Classify this ticket as bug or feature", "Is this sentence positive or negative?",
                            "Label these commits by type", "Triage the incoming issue: yes or no"],
}


def seed_dataset(seed: int = 7, verification_n: int = 160) -> list[DatasetItem]:
    rng = random.Random(seed)
    items: list[DatasetItem] = []
    for i in range(verification_n):
        iteration = rng.randint(0, 4)
        base = {"mode": "build", "iteration": iteration, "max_iterations": 6, "requires_tests": True,
                "requires_changes": True, "same_agent_failures": 0, "agent_status": "done"}
        case = i % 10
        if case in (0, 1, 2):
            ev = [_tests(rng.randint(1, 30)), _diff(rng.randint(1, 6))]
            if case == 2:
                ev.append(_lint(0))
            items.append(DatasetItem(id=f"v{i}", type="verification", state=base, evidence=ev, label="confirm_done"))
        elif case == 3:  # agent claims success but tests fail
            ev = [_tests(rng.randint(0, 10), failed=rng.randint(1, 5)), _diff(2)]
            items.append(DatasetItem(id=f"v{i}", type="verification", state=base, evidence=ev,
                                     label="restart_same_agent"))
        elif case == 4:  # repeated failures → reassign
            ev = [_tests(3, failed=2), _diff(1)]
            items.append(DatasetItem(id=f"v{i}", type="verification", state={**base, "same_agent_failures": 2},
                                     evidence=ev, label="reassign_new_agent"))
        elif case == 5:  # empty diff
            items.append(DatasetItem(id=f"v{i}", type="verification", state=base,
                                     evidence=[_tests(4), _diff(0)], label="restart_same_agent"))
        elif case == 6:  # no tests ran
            items.append(DatasetItem(id=f"v{i}", type="verification", state=base, evidence=[_diff(3)],
                                     label="restart_same_agent"))
        elif case == 7:  # ceiling reached while failing
            items.append(DatasetItem(id=f"v{i}", type="verification", state={**base, "iteration": 5},
                                     evidence=[_tests(1, failed=1), _diff(1)], label="escalate"))
        elif case == 8:  # timeout
            items.append(DatasetItem(id=f"v{i}", type="verification", state=base,
                                     evidence=[_tests(0, timed_out=True, exit_code=124), _diff(1)],
                                     label="restart_same_agent"))
        else:  # screen test
            ok = rng.random() < 0.5
            items.append(DatasetItem(
                id=f"v{i}", type="verification", state={**base, "requires_tests": False},
                evidence=[_screen(3, 0 if ok else 1), _diff(2)], label="confirm_done" if ok else "restart_same_agent"))
    for category, tasks in ROUTING_SEED.items():
        for j, task in enumerate(tasks):
            mode = {"research_retrieval": "research", "heavy_reasoning": "analysis"}.get(category, "build")
            if category == "fast_classification":
                mode = "chat"
            items.append(DatasetItem(id=f"r-{category}-{j}", type="routing", state={"task": task, "mode": mode},
                                     label=category))
    return items


# --------------------------------------------------------------------------- export from the decision log
def _from_logged(row: Decision) -> DatasetItem | None:
    state = dict(row.input_state_json or {})
    question = state.get("question") or {}
    evidence: list[EvidenceSpec] = []
    for item in state.get("evidence", []):
        try:
            EvidenceItem.model_validate(item)  # only re-export evidence whose signature still verifies
        except ValueError:
            continue
        evidence.append(EvidenceSpec(kind=item["kind"], source=item["source"], payload=item["payload"]))
    label = row.label
    if label is None:
        return None
    return DatasetItem(id=row.id, type=row.type, state=question.get("state", {}), evidence=evidence, label=label,
                       origin="logged")


async def export_logged(sessions: async_sessionmaker[AsyncSession], path: Path,
                        include_engine_choices: bool = False) -> tuple[Path, int]:
    """Export labelled decisions (user labels/escalation answers) as JSONL.

    With ``include_engine_choices``, unlabelled *acted* rules decisions are added with the rules choice as a
    weak label (useful to bootstrap Laya; never used to validate it)."""
    async with sessions() as session:
        rows = list(await session.scalars(select(Decision).order_by(Decision.created_at)))
    items: list[DatasetItem] = []
    for row in rows:
        item = _from_logged(row)
        if item is None and include_engine_choices and row.acted and row.engine == "rules":
            row.label = row.choice
            item = _from_logged(row)
            if item is not None:
                item.origin = "weak"
        if item is not None:
            items.append(item)
    write_jsonl(items, path)
    return path, len(items)


def to_laya_records(items: list[DatasetItem]) -> list[tuple[str, dict[str, Any], dict[str, list[float]]]]:
    """(state, questions, targets) triples in the format laya's calibration/training code consumes."""
    from backend.decision.laya_engine import laya_questions, state_text

    out = []
    for item in items:
        question = item.question()
        options = list(question.options)
        target = [1.0 if o == item.label else 0.0 for o in options]
        out.append((state_text(question, item.evidence_obj()), laya_questions(question), {"decision": target}))
    return out


def dump_laya_jsonl(items: list[DatasetItem], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for state, questions, targets in to_laya_records(items):
            handle.write(json.dumps({"state": state, "questions": questions, "targets": targets}) + "\n")
    return path
