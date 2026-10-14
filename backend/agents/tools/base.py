"""Tool contract, per-call context (permissions, workspace, sandbox, approvals) and the registry."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.agents.config import AgentConfig
from backend.core.errors import AppError, PermissionDenied
from backend.core.ids import utcnow
from backend.core.logging import get_logger
from backend.core.paths import WorkspaceGuard
from backend.db.models import EscalationRequest
from backend.decision.schemas import EvidenceItem
from backend.providers.base import ToolSpec

if TYPE_CHECKING:
    from backend.agents.pool import CallNode, RunScope
    from backend.app_context import AppContext
    from backend.orchestrator.recorder import RunRecorder
    from backend.sandbox.base import Sandbox

log = get_logger(__name__)


class ToolResult(BaseModel):
    ok: bool = True
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)


@dataclass
class ToolContext:
    app: AppContext
    run_id: str
    user_id: str
    call_id: str
    agent: AgentConfig
    node: CallNode
    scope: RunScope
    recorder: RunRecorder
    workspace: Path
    guard: WorkspaceGuard
    sandbox_factory: Callable[[], Awaitable[Sandbox]]
    tool_call_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def settings(self) -> Any:
        return self.app.settings

    async def sandbox(self) -> Sandbox:
        return await self.sandbox_factory()

    def emit(self, type: str, data: dict[str, Any]) -> None:
        self.recorder.emit(type, data, call_id=self.call_id,
                           parent_call_id=self.node.parent.call_id if self.node.parent else None)

    def emit_threadsafe(self, type: str, data: dict[str, Any]) -> None:
        self.app.events.publish_threadsafe(type, user_id=self.user_id, run_id=self.run_id, call_id=self.call_id,
                                           data=data)

    def file_changed(self, path: str, change: str, size: int | None = None) -> None:
        from backend.core.paths import detect_language

        self.emit("file.changed", {"path": path, "change": change, "size": size, "language": detect_language(path)})

    async def request_approval(self, action: str, details: dict[str, Any], reasons: list[str]) -> bool:
        """Pause the run (``awaiting_user``) until the user approves or denies a risky action."""
        fingerprint = hashlib.sha1(json.dumps({"a": action, "d": details}, sort_keys=True, default=str)
                                   .encode(), usedforsecurity=False).hexdigest()
        async with self.app.sessions() as session:
            prior = await session.scalar(select(EscalationRequest).where(
                EscalationRequest.run_id == self.run_id, EscalationRequest.kind == "approval",
                EscalationRequest.status == "resolved").order_by(EscalationRequest.created_at.desc()))
            if (prior is not None and (prior.context or {}).get("fingerprint") == fingerprint
                    and prior.resolved_choice == "approve"):
                return True  # approved before (e.g. before a restart); don't ask twice. Denials are re-asked.
        question = f"Approve {action}? Reason: {', '.join(reasons) or 'policy requires approval'}"
        esc_id = await self.recorder.open_escalation(
            question=question, options=["approve", "deny"], kind="approval", call_id=self.call_id,
            context={"action": action, "details": details, "reasons": reasons, "fingerprint": fingerprint})
        await self.recorder.set_status("awaiting_user")
        answer = await self.app.approvals.wait(esc_id, timeout=self.app.settings.approval_timeout_s)
        if answer is None:
            async with self.app.sessions() as session:
                esc = await session.get(EscalationRequest, esc_id)
                if esc is not None and esc.status == "open":
                    esc.status = "expired"
                    esc.resolved_at = utcnow()
                    await session.commit()
        await self.recorder.set_status("running")
        return bool(answer and answer.get("choice") == "approve")


class Tool(ABC):
    name: str
    description: str
    permission: str
    parameters: dict[str, Any]

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    @abstractmethod
    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult: ...


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


ToolProvider = Callable[[ToolContext], Awaitable[list[Tool]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._providers: list[ToolProvider] = []

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add_provider(self, provider: ToolProvider) -> None:
        """Dynamic tools (e.g. MCP servers) resolved per call."""
        self._providers.append(provider)

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def available(self, ctx: ToolContext) -> list[Tool]:
        tools = [t for t in self._tools.values() if ctx.agent.has(t.permission)]
        for provider in self._providers:
            try:
                tools.extend(t for t in await provider(ctx) if ctx.agent.has(t.permission))
            except Exception:
                log.exception("tool provider failed")
        return tools

    async def lookup(self, ctx: ToolContext, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        for provider in self._providers:
            try:
                for tool in await provider(ctx):
                    if tool.name == name:
                        return tool
            except Exception:
                log.exception("tool provider failed")
        return None

    async def invoke(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolResult:
        metrics = ctx.app.metrics
        tool = await self.lookup(ctx, name)
        if tool is None:
            return ToolResult(ok=False, content=f"error: unknown tool '{name}'")
        if not ctx.agent.has(tool.permission):
            metrics.tool_calls.labels(name, "denied").inc()
            log.warning("agent %s denied tool %s (needs %s)", ctx.agent.name, name, tool.permission)
            return ToolResult(ok=False, content=f"permission denied: agent '{ctx.agent.name}' lacks "
                                                f"'{tool.permission}' required by {name}")
        missing = [k for k in tool.parameters.get("required", []) if k not in args]
        if missing:
            return ToolResult(ok=False, content=f"error: missing required argument(s): {', '.join(missing)}")
        if "__unparsed__" in args:
            return ToolResult(ok=False, content="error: tool arguments were not valid JSON; call the tool again")
        try:
            result = await tool.run(ctx, args)
        except PermissionDenied as exc:
            metrics.tool_calls.labels(name, "denied").inc()
            return ToolResult(ok=False, content=f"permission denied: {exc.message}")
        except AppError as exc:
            metrics.tool_calls.labels(name, "error").inc()
            return ToolResult(ok=False, content=f"error: {exc.message}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            metrics.tool_calls.labels(name, "error").inc()
            return ToolResult(ok=False, content=f"error: {type(exc).__name__}: {exc}")
        metrics.tool_calls.labels(name, "ok" if result.ok else "error").inc()
        return result
