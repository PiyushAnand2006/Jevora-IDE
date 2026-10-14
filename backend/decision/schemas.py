"""Typed decision schemas and the Evidence type.

Verification input is **evidence only**: test/lint/type-check output, diffs, execution logs,
screenshots/click logs. ``EvidenceItem`` instances carry an HMAC signature over their content
that only the tool runtime can produce (``EvidenceFactory``), so an item cannot be constructed
from an agent's narrative — neither by the agent nor by accident in orchestration code. The
``Evidence`` container forbids unknown fields and rejects raw strings / narrative objects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- choice taxonomies
VerificationChoice = Literal["confirm_done", "restart_same_agent", "reassign_new_agent", "escalate"]
RoutingChoice = Literal["heavy_reasoning", "fast_classification", "code_generation", "creative_ui",
                        "research_retrieval"]
NextActionChoice = Literal["plan", "code", "design_ui", "research", "analyze", "test", "critique", "verify",
                           "finalize", "escalate"]
AssignmentChoice = Literal["planner", "coder", "tester", "critic", "ui-designer", "researcher", "analyst",
                           "screen-tester", "chat"]
RetryChoice = Literal["retry_same", "retry_stronger_model", "give_up", "escalate"]
CompletionChoice = Literal["complete", "incomplete", "escalate"]

VERIFICATION_CHOICES: tuple[str, ...] = ("confirm_done", "restart_same_agent", "reassign_new_agent", "escalate")
ROUTING_CHOICES: tuple[str, ...] = ("heavy_reasoning", "fast_classification", "code_generation", "creative_ui",
                                    "research_retrieval")
NEXT_ACTION_CHOICES: tuple[str, ...] = ("plan", "code", "design_ui", "research", "analyze", "test", "critique",
                                        "verify", "finalize", "escalate")
ASSIGNMENT_CHOICES: tuple[str, ...] = ("planner", "coder", "tester", "critic", "ui-designer", "researcher",
                                       "analyst", "screen-tester", "chat")
RETRY_CHOICES: tuple[str, ...] = ("retry_same", "retry_stronger_model", "give_up", "escalate")
COMPLETION_CHOICES: tuple[str, ...] = ("complete", "incomplete", "escalate")

DecisionType = Literal["verification", "routing", "next_action", "assignment", "retry", "completion"]
SCHEMA_CHOICES: dict[str, tuple[str, ...]] = {
    "verification": VERIFICATION_CHOICES,
    "routing": ROUTING_CHOICES,
    "next_action": NEXT_ACTION_CHOICES,
    "assignment": ASSIGNMENT_CHOICES,
    "retry": RETRY_CHOICES,
    "completion": COMPLETION_CHOICES,
}

# --------------------------------------------------------------------------- evidence payloads
EvidenceKind = Literal["test_result", "lint", "type_check", "diff", "execution_log", "screenshot",
                       "static_analysis", "citations"]
EvidenceSource = Literal["tool_execution", "sandbox", "screen_driver", "workspace_diff", "static_analyzer",
                         "retrieval"]


class TestResultPayload(BaseModel):
    __test__ = False  # not a pytest test class
    framework: str
    command: str
    exit_code: int | None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0
    timed_out: bool = False
    duration_s: float = 0.0
    output_tail: str = ""
    failures: list[str] = Field(default_factory=list)


class ExecutionLogPayload(BaseModel):
    command: str
    exit_code: int | None
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_s: float = 0.0


class DiffPayload(BaseModel):
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    diff_tail: str = ""

    @property
    def empty(self) -> bool:
        return self.files_changed == 0


class LintPayload(BaseModel):
    tool: str
    errors: int = 0
    warnings: int = 0
    exit_code: int | None = None
    output_tail: str = ""


class Assertion(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ScreenshotPayload(BaseModel):
    url: str = ""
    screenshots: list[dict[str, str]] = Field(default_factory=list)  # {path, sha256}
    assertions: list[Assertion] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    network_errors: list[str] = Field(default_factory=list)
    steps: int = 0
    stopped_reason: str = ""
    action_log: list[dict[str, Any]] = Field(default_factory=list)


class StaticAnalysisPayload(BaseModel):
    files: int = 0
    lines: int = 0
    findings: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)


class CitationsPayload(BaseModel):
    sources_available: int = 0
    sources_cited: int = 0
    unresolved_citations: int = 0


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "test_result": TestResultPayload, "lint": LintPayload, "type_check": LintPayload, "diff": DiffPayload,
    "execution_log": ExecutionLogPayload, "screenshot": ScreenshotPayload,
    "static_analysis": StaticAnalysisPayload, "citations": CitationsPayload,
}


# --------------------------------------------------------------------------- signing
class _EvidenceKey:
    value: bytes = secrets.token_bytes(32)


def configure_evidence_key(data_dir: Path) -> None:
    """Load (or create) the persistent signing key so checkpointed evidence survives restarts."""
    from backend.core.crypto import _write_private_file

    path = data_dir / "evidence.key"
    if path.exists():
        _EvidenceKey.value = bytes.fromhex(path.read_text(encoding="ascii").strip())
    else:
        _write_private_file(path, _EvidenceKey.value.hex().encode("ascii"))


def _canonical(kind: str, source: str, payload: dict[str, Any], tool_call_id: str | None) -> bytes:
    return json.dumps({"k": kind, "s": source, "p": payload, "t": tool_call_id}, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def _sign(kind: str, source: str, payload: dict[str, Any], tool_call_id: str | None) -> str:
    return hmac.new(_EvidenceKey.value, _canonical(kind, source, payload, tool_call_id), hashlib.sha256).hexdigest()


class EvidenceItem(BaseModel):
    """One piece of machine-produced evidence. Construction requires a valid runtime signature.

    ``revalidate_instances="always"`` means even an object smuggled in via ``model_construct``
    is re-checked (signature included) when placed into an ``Evidence`` container.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    kind: EvidenceKind
    source: EvidenceSource
    payload: dict[str, Any]
    tool_call_id: str | None = None
    signature: str

    @model_validator(mode="after")
    def _verify(self) -> EvidenceItem:
        PAYLOAD_MODELS[self.kind].model_validate(self.payload)
        expected = _sign(self.kind, self.source, self.payload, self.tool_call_id)
        if not hmac.compare_digest(expected, self.signature):
            raise ValueError("evidence signature invalid: evidence must come from tool execution, "
                             "not from agent narrative")
        return self

    def typed(self) -> Any:
        return PAYLOAD_MODELS[self.kind].model_validate(self.payload)


class EvidenceFactory:
    """The only producer of valid evidence; used by tools, the sandbox and the screen driver."""

    @staticmethod
    def make(kind: str, source: str, payload: BaseModel, tool_call_id: str | None = None) -> EvidenceItem:
        data = payload.model_dump(mode="json")
        return EvidenceItem(kind=kind, source=source, payload=data,
                            tool_call_id=tool_call_id, signature=_sign(kind, source, data, tool_call_id))


class AgentNarrative(BaseModel):
    """What an agent *says* it did. Deliberately unusable as evidence."""

    text: str


_NARRATIVE_KEYS = {"self_report", "narrative", "claim", "summary", "agent_output", "message", "text"}


class Evidence(BaseModel):
    """Container handed to ``verify_work``. Only signed ``EvidenceItem`` objects are accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_narrative(cls, value: Any) -> Any:
        if isinstance(value, (str, AgentNarrative)):
            raise ValueError("Evidence cannot be built from agent narrative text")
        if isinstance(value, dict) and _NARRATIVE_KEYS & set(value):
            raise ValueError("Evidence cannot contain agent self-reports")
        return value

    @field_validator("items", mode="before")
    @classmethod
    def _items_are_evidence(cls, value: Any) -> Any:
        for item in value or []:
            if isinstance(item, (str, AgentNarrative)):
                raise ValueError("Evidence items must be machine-produced EvidenceItem objects")
        return value

    def of_kind(self, kind: str) -> list[EvidenceItem]:
        return [i for i in self.items if i.kind == kind]

    def latest(self, kind: str) -> Any | None:
        found = self.of_kind(kind)
        return found[-1].typed() if found else None

    def refs(self) -> list[str]:
        return [f"{i.kind}:{i.signature[:12]}" for i in self.items]

    def merged(self, more: list[EvidenceItem]) -> Evidence:
        return Evidence(items=[*self.items, *more])


# --------------------------------------------------------------------------- decision I/O
class DecisionQuestion(BaseModel):
    """A typed question for the engine. ``state`` holds structured, non-narrative features."""

    type: DecisionType
    options: list[str]
    state: dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    high_risk: bool = False


class DecisionResult(BaseModel):
    type: DecisionType
    choice: str
    confidence: float = Field(ge=0.0, le=1.0)
    scores_per_option: dict[str, float] = Field(default_factory=dict)
    latency_ms: float = 0.0
    engine: str
    fallback_used: bool = False
    shadow: bool = False
    reason: str = ""
    shadow_results: list[DecisionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def _choice_in_options(self) -> DecisionResult:
        allowed = SCHEMA_CHOICES[self.type]
        if self.choice not in allowed:
            raise ValueError(f"choice '{self.choice}' is not valid for {self.type}: {allowed}")
        return self
