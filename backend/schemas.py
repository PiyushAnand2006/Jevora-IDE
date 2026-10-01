"""Shared typed schemas (PRD §3.2 — decision schemas are fixed taxonomies)."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Decision layer (architecture §2.3)
# ---------------------------------------------------------------------------

class WorkVerdict(str, Enum):
    confirm_done = "confirm_done"
    restart_same_agent = "restart_same_agent"
    reassign_new_agent = "reassign_new_agent"
    escalate = "escalate"


class TaskCategory(str, Enum):
    heavy_reasoning = "heavy_reasoning"
    fast_classification = "fast_classification"
    code_generation = "code_generation"
    creative_ui = "creative_ui"
    research_retrieval = "research_retrieval"


class Decision(BaseModel):
    """A typed, non-generative answer from the decision layer."""
    kind: Literal["work_verification", "model_routing", "next_action", "retry", "completion"]
    choice: str
    confidence: float = Field(ge=0.0, le=1.0)
    engine: str
    rationale: str = ""
    escalate: bool = False


class Evidence(BaseModel):
    """Tool output the decision layer judges — never agent self-report."""
    test_passed: Optional[bool] = None
    tests_run: int = 0
    lint_ok: Optional[bool] = None
    files_changed: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    exec_log_tail: str = ""
    screenshots: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    text: str
    mode: Literal["build", "chat", "research", "analysis"] = "build"
    attachments: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agents (PRD §3.1)
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    name: str
    template: str = "coder"
    system_prompt: str = ""
    tool_permissions: list[str] = Field(default_factory=list)
    model_binding: Optional[str] = None   # explicit model, else router decides
    memory_scope: Literal["task", "session", "global"] = "task"


class AgentRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent"))
    parent_id: Optional[str] = None       # subagent call chain (§3.1)
    config: AgentConfig
    status: Literal["queued", "running", "done", "failed", "escalated"] = "queued"
    model_used: Optional[str] = None
    started_at: float = Field(default_factory=now)
    finished_at: Optional[float] = None
    output: str = ""


# ---------------------------------------------------------------------------
# Providers (PRD §3.3)
# ---------------------------------------------------------------------------

class Provider(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prov"))
    name: str
    base_url: str
    api_key: str = ""
    kind: Literal["hosted", "local", "custom"] = "custom"
    created_at: float = Field(default_factory=now)


# ---------------------------------------------------------------------------
# Streaming events to the client
# ---------------------------------------------------------------------------

class StreamEvent(BaseModel):
    run_id: str
    type: Literal[
        "run_started", "agent_started", "agent_token", "agent_finished",
        "subagent_called", "decision", "node_status", "run_finished",
        "error", "info",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=now)


class TaskRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    spec: TaskSpec
    status: Literal["running", "done", "failed", "escalated"] = "running"
    agents: list[AgentRun] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    final_output: str = ""
    created_at: float = Field(default_factory=now)
    finished_at: Optional[float] = None
