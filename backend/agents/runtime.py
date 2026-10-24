"""Agent runtime: the model ⇄ tool loop for one agent call, plus recursive subagents.

Every call is recorded as an ``AgentCall`` (with ``parent_call_id``/depth/lineage), runs as its
own asyncio task registered on a ``CallNode`` (so cancelling a parent cancels all descendants),
is bounded by agent + run budgets, and detects tool-call loops.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.agents.config import AgentConfig
from backend.agents.context import project_history
from backend.agents.memory import MemoryService
from backend.agents.pool import CallNode, RunScope, task_key
from backend.agents.templates import BUILTIN_TEMPLATES, template
from backend.agents.tools.base import Tool, ToolContext, ToolRegistry, ToolResult, schema
from backend.agents.workspace_state import WorkspaceSnapshot
from backend.core.errors import BudgetExceeded
from backend.core.logging import correlation, get_logger
from backend.core.paths import WorkspaceGuard
from backend.decision.schemas import EvidenceItem
from backend.orchestrator.recorder import RunRecorder
from backend.providers.base import ChatMessage, ChatResponse, ContentPart, ProviderError, ToolCall, ToolSpec
from backend.providers.router import Binding, CallMeta

if TYPE_CHECKING:
    from backend.app_context import AppContext
    from backend.sandbox.base import Sandbox

log = get_logger(__name__)
TOOL_RESULT_CHARS = 12_000
LOOP_WARN_AT = 3
LOOP_ABORT_AT = 6
_TEXT_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


@dataclass
class RunContext:
    """Everything an agent needs to know about the run it belongs to."""

    app: AppContext
    run_id: str
    user_id: str
    mode: str
    task: str
    recorder: RunRecorder
    scope: RunScope
    workspace: Path
    model_override: str | None = None
    snapshot: WorkspaceSnapshot | None = None
    flags: dict[str, bool] = field(default_factory=dict)
    image_parts: list[ContentPart] = field(default_factory=list)
    route_fn: Callable[[str, str], Awaitable[str]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def guard(self) -> WorkspaceGuard:
        return WorkspaceGuard(self.workspace)

    async def sandbox(self) -> Sandbox:
        return await self.app.sandboxes.for_run(self.run_id, self.user_id, self.workspace)


@dataclass
class AgentResult:
    call_id: str
    status: str
    output: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    model: str | None = None
    provider_id: str | None = None
    tokens: int = 0
    cost: float = 0.0
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)


class AgentFailed(Exception):
    pass


def _text_tool_prompt(tools: list[ToolSpec]) -> str:
    lines = ["You can call tools. To call one, reply with exactly one block of the form",
             '<tool_call>{"name": "<tool name>", "arguments": {...}}</tool_call>',
             "and nothing else. After the result arrives you may call another tool or give the final answer "
             "as plain text. Available tools:"]
    for tool in tools:
        lines.append(f"- {tool.name}: {tool.description} Parameters JSON schema: {json.dumps(tool.parameters)}")
    return "\n".join(lines)


def parse_inline_tool_calls(content: str, names: set[str]) -> list[ToolCall]:
    """Recover tool calls that a model wrote into its text instead of using native tool calling."""
    calls: list[ToolCall] = []
    candidates = _TEXT_TOOL_RE.findall(content)
    stripped = content.strip()
    if not candidates and stripped.startswith("{") and stripped.endswith("}"):
        candidates = [stripped]
    for index, raw in enumerate(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            from backend.providers.jsonrepair import repair_json

            try:
                data = repair_json(raw)
            except ValueError:
                continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or data.get("tool")
        args = data.get("arguments", data.get("parameters", data.get("args", {})))
        if name in names and isinstance(args, dict):
            calls.append(ToolCall(id=f"text_call_{index}", name=str(name), arguments=args))
    return calls


class AgentRuntime:
    def __init__(self, app: AppContext, tools: ToolRegistry, memory: MemoryService) -> None:
        self.app = app
        self.tools = tools
        self.memory = memory

    # ------------------------------------------------------------------ model selection
    async def bindings_for(self, rc: RunContext, config: AgentConfig, task: str,
                           category: str | None, web_search: bool = False) -> tuple[list[Binding], str]:
        router = self.app.router
        requested_category = "research_retrieval" if web_search else (category or config.category or "code_generation")
        if rc.model_override and config.template != "screen-tester":
            return await router.resolve(rc.user_id, rc.model_override, requested_category,
                                        require_capability="web_search" if web_search else None), "explicit"
        if config.model_binding.mode == "explicit":
            return await router.resolve(rc.user_id, config.model_binding.spec(),
                                        requested_category,
                                        require_capability="web_search" if web_search else None), "explicit"
        if web_search:
            return await router.resolve(rc.user_id, "auto", requested_category,
                                        require_capability="web_search"), requested_category
        chosen = category or config.category
        if chosen is None:
            chosen = await rc.route_fn(task, config.name) if rc.route_fn is not None else "code_generation"
        if not rc.flags.get("dynamic_model_router", True) and chosen != "computer_use":
            # Dynamic routing disabled: every agent uses one default binding.
            return await router.resolve(rc.user_id, "auto", "code_generation"), "code_generation"
        return await router.resolve(rc.user_id, "auto", chosen), chosen

    # ------------------------------------------------------------------ prompt
    async def _system_prompt(self, rc: RunContext, config: AgentConfig, task: str, sandbox_kind: str) -> str:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        parts = [prompt.strip() for prompt in (self.app.settings.acting_brain_system_prompt, config.system_prompt)
                 if prompt.strip()]
        parts.append(
            f"Environment: workspace root is '.', commands run in a {sandbox_kind} sandbox; today is {today}."
        )
        if config.memory_scope == "agent":
            notes = await self.memory.agent_notes(rc.user_id, config.name)
            if notes:
                parts.append("Notes from your previous runs (most recent last):\n" +
                             "\n".join(f"- {n[:600]}" for n in notes))
        elif config.memory_scope == "workspace" and task.strip():
            try:
                await self.memory.index_workspace(rc.user_id, rc.workspace)
                snippets = await self.memory.search_workspace(rc.user_id, rc.workspace, task, k=4)
            except Exception:
                log.exception("workspace memory lookup failed")
                snippets = []
            if snippets:
                parts.append("Relevant project knowledge:\n" + "\n\n".join(
                    f"[{s.source}]\n{s.content[:1200]}" for s in snippets))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ public entry
    async def run_agent(self, rc: RunContext, config: AgentConfig, task: str, *, role: str | None = None,
                        parent: CallNode | None = None, context: str = "", category: str | None = None,
                        stream: bool = False, agent_id: str | None = None,
                        history: list[ChatMessage] | None = None, include_images: bool = False,
                        web_search: bool = False) -> AgentResult:
        role = role or config.template or config.name
        depth = 0 if parent is None else parent.depth + 1
        call_id = await rc.recorder.start_call(
            role=role, agent_name=config.name, task=task[:20_000], parent_call_id=parent.call_id if parent else None,
            depth=depth, agent_id=agent_id, category=category or config.category,
            lineage=[*parent.lineage(), parent.call_id] if parent else [])
        node = CallNode(call_id=call_id, config=config, key=task_key(config, task), depth=depth, parent=parent)
        self.app.agent_pool.enter(rc.scope, node)
        body = asyncio.create_task(
            self._run(rc, node, config, task, role, context, category, stream, history, include_images, web_search),
            name=f"agent-{call_id}")
        node.task = body
        try:
            return await body
        except asyncio.CancelledError:
            await rc.recorder.finish_call(call_id, status="cancelled", error="cancelled")
            raise
        finally:
            self.app.agent_pool.exit(rc.scope, node)

    async def _run(self, rc: RunContext, node: CallNode, config: AgentConfig, task: str, role: str, context: str,
                   category: str | None, stream: bool, history: list[ChatMessage] | None,
                   include_images: bool, web_search: bool) -> AgentResult:
        with correlation(run_id=rc.run_id, call_id=node.call_id):
            result = AgentResult(call_id=node.call_id, status="running", output="")
            try:
                await self._loop(rc, node, config, task, role, context, category, stream, history, include_images,
                                 result, web_search)
                result.status = "done"
            except BudgetExceeded as exc:
                result.status, result.error = "failed", exc.message
            except AgentFailed as exc:
                result.status, result.error = "failed", str(exc)
            except ProviderError as exc:
                result.status, result.error = "failed", f"model call failed: {exc}"
            if result.status == "failed" and not result.output:
                result.output = f"[{role} failed] {result.error}"
            await rc.recorder.finish_call(node.call_id, status=result.status, output=result.output,
                                          error=result.error)
            if result.status == "done" and config.memory_scope == "agent":
                await self.memory.remember_agent(rc.user_id, config.name, rc.run_id,
                                                 f"Task: {task[:300]}\nOutcome: {result.output[:1200]}")
            return result

    async def _loop(self, rc: RunContext, node: CallNode, config: AgentConfig, task: str, role: str, context: str,
                    category: str | None, stream: bool, history: list[ChatMessage] | None, include_images: bool,
                    result: AgentResult, web_search: bool) -> None:
        started = time.monotonic()
        try:
            sandbox_kind = (await self.app.sandboxes.status())["kind"] or "unavailable"
        except Exception:
            sandbox_kind = "unavailable"
        tool_ctx = ToolContext(app=self.app, run_id=rc.run_id, user_id=rc.user_id, call_id=node.call_id,
                               agent=config, node=node, scope=rc.scope, recorder=rc.recorder,
                               workspace=rc.workspace, guard=rc.guard, sandbox_factory=rc.sandbox,
                               extras={"runtime": self, "run_context": rc})
        tools = await self.tools.available(tool_ctx)
        # Native provider web search is controlled by the run flag and does not
        # need the legacy application-side search tool in the model's tool list.
        if web_search:
            tools = [tool for tool in tools if tool.name != "web_search"]
        specs = [t.spec() for t in tools]
        names = {t.name for t in tools}
        bindings, chosen_category = await self.bindings_for(rc, config, task, category, web_search=web_search)
        system = await self._system_prompt(rc, config, task, sandbox_kind)
        user_text = task if not context.strip() else f"{task}\n\n--- Context ---\n{context}"
        first: ChatMessage
        if include_images and rc.image_parts:
            first = ChatMessage(role="user", content=[ContentPart(type="text", text=user_text), *rc.image_parts])
        else:
            first = ChatMessage(role="user", content=user_text)
        messages = [ChatMessage(role="system", content=system), first]
        text_mode = False
        signatures: Counter[str] = Counter()
        meta = CallMeta(user_id=rc.user_id, run_id=rc.run_id, call_id=node.call_id, category=chosen_category)
        model_recorded = False

        async def on_delta(text: str) -> None:
            rc.recorder.emit("token.delta", {"text": text, "role": role}, call_id=node.call_id,
                             parent_call_id=node.parent.call_id if node.parent else None)

        async def on_reset() -> None:
            rc.recorder.emit("token.reset", {"role": role, "reason": "provider failed mid-stream; retrying"},
                             call_id=node.call_id)

        async def on_usage(response: ChatResponse, binding: Binding) -> None:
            tokens = response.usage.total
            rc.scope.budget.add(tokens, response.cost_usd)
            result.tokens += tokens
            result.cost += response.cost_usd
            await rc.recorder.add_call_usage(node.call_id, tokens, response.cost_usd)
            await rc.recorder.add_usage(tokens, response.cost_usd)

        for step in range(config.budget.max_steps):
            if node.cancelled or rc.scope.cancelled:
                raise asyncio.CancelledError
            rc.scope.budget.check()
            if result.tokens > config.budget.max_tokens:
                raise BudgetExceeded(f"agent token budget exceeded ({result.tokens})")
            if time.monotonic() - started > config.budget.max_seconds:
                raise BudgetExceeded(f"agent time budget exceeded ({config.budget.max_seconds}s)")
            result.steps = step + 1
            convo = messages
            if text_mode:
                convo = [ChatMessage(role="system", content=system + "\n\n" + _text_tool_prompt(specs)),
                         *messages[1:]]
            if history:
                projected = project_history(history, convo, [] if text_mode else specs,
                                            self.app.settings.context_window_tokens)
                convo = [convo[0], *projected, *convo[1:]]
            try:
                response = await self.app.router.chat(
                    meta, convo, bindings=bindings, tools=None if text_mode else (specs or None), stream=stream,
                    on_delta=on_delta if stream else None, on_reset=on_reset, on_usage=on_usage,
                    temperature=config.temperature,
                    require_capability="web_search" if web_search and step == 0 else None)
            except ProviderError as exc:
                if not text_mode and specs and exc.status == 400:
                    # Many local/OpenAI-compatible servers reject native tool calling with a 400.
                    log.info("model rejected native tools; switching %s to text tool mode", node.call_id)
                    text_mode = True
                    continue
                raise
            if not model_recorded:
                await rc.recorder.set_call_model(node.call_id, response.model, response.provider_id)
                result.model, result.provider_id = response.model, response.provider_id
                model_recorded = True
            for source in response.sources:
                if source.get("location") and all(existing.get("location") != source["location"]
                                                  for existing in result.sources):
                    result.sources.append(source)
            calls = response.tool_calls
            if not calls and names:
                calls = parse_inline_tool_calls(response.content, names)
            if not calls:
                result.output = response.content.strip()
                return
            messages.append(ChatMessage(role="assistant", content="" if not text_mode else response.content,
                                        tool_calls=[] if text_mode else calls))
            outcomes = await self._execute_calls(tool_ctx, calls, signatures, result)
            for call, outcome in zip(calls, outcomes, strict=True):
                content = outcome.content if len(outcome.content) <= TOOL_RESULT_CHARS else (
                    outcome.content[:TOOL_RESULT_CHARS] + "\n…[truncated]")
                if text_mode:
                    messages.append(ChatMessage(role="user", content=f"Result of {call.name}:\n{content}"))
                else:
                    messages.append(ChatMessage(role="tool", content=content, tool_call_id=call.id, name=call.name))
            if result.tool_calls > config.budget.max_tool_calls:
                raise BudgetExceeded(f"agent tool-call budget exceeded ({result.tool_calls})")
        result.output = (result.output or "") + "\n[stopped: step limit reached]"

    async def _execute_calls(self, ctx: ToolContext, calls: list[ToolCall], signatures: Counter[str],
                             result: AgentResult) -> list[ToolResult]:
        async def one(call: ToolCall) -> ToolResult:
            signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
            signatures[signature] += 1
            seen = signatures[signature]
            if seen >= LOOP_ABORT_AT:
                raise AgentFailed(f"tool-call loop detected: {call.name} repeated {seen} times with identical "
                                  "arguments")
            call_ctx = dataclasses.replace(ctx, tool_call_id=call.id)
            ctx.emit("tool.call", {"tool": call.name, "arguments": _preview_args(call.arguments), "id": call.id})
            if seen >= LOOP_WARN_AT:
                outcome = ToolResult(ok=False, content=(
                    f"warning: you already called {call.name} with identical arguments {seen - 1} times; the "
                    "result will not change. Use a different approach or give your final answer."))
            else:
                outcome = await self.tools.invoke(call_ctx, call.name, call.arguments)
            result.tool_calls += 1
            result.evidence.extend(outcome.evidence)
            for path in outcome.files_changed:
                if path not in result.files_changed:
                    result.files_changed.append(path)
            ctx.emit("tool.result", {"tool": call.name, "id": call.id, "ok": outcome.ok,
                                     "preview": outcome.content[:600], "data": _preview_args(outcome.data)})
            return outcome

        spawns = [c for c in calls if c.name == "spawn_subagent"]
        if len(spawns) == len(calls) and len(calls) > 1:
            return list(await asyncio.gather(*(one(c) for c in calls)))  # parallel subagents
        return [await one(c) for c in calls]


def _preview_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + f"…[{len(value)} chars]"
        elif isinstance(value, (list, dict)) and len(json.dumps(value, default=str)) > 800:
            out[key] = f"[{type(value).__name__} with {len(value)} items]"
        else:
            out[key] = value
    return out


class SpawnSubagent(Tool):
    name = "spawn_subagent"
    description = ("Delegate a well-scoped subtask to a subagent and wait for its result. Use template for a "
                   "built-in role (planner, coder, tester, critic, ui-designer, researcher, analyst) or pass an "
                   "inline agent config. The subagent can never have more permissions than you.")
    permission = "subagent.spawn"
    parameters = schema({
        "template": {"type": "string", "enum": [k for k in BUILTIN_TEMPLATES if k != "screen-tester"]},
        "task": {"type": "string"},
        "context": {"type": "string"},
        "agent": {"type": "object", "properties": {
            "name": {"type": "string"}, "system_prompt": {"type": "string"},
            "tool_permissions": {"type": "array", "items": {"type": "string"}}}},
    }, ["task"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        runtime: AgentRuntime = ctx.extras["runtime"]
        rc: RunContext = ctx.extras["run_context"]
        parent = ctx.node
        settings = ctx.settings
        task = str(args.get("task") or "").strip()
        if not task:
            return ToolResult(ok=False, content="error: task is required")
        depth = parent.depth + 1
        if depth > settings.max_subagent_depth or parent.config.max_depth < 1:
            return ToolResult(ok=False, content=f"refused: maximum subagent depth reached ({depth - 1})")
        fanout_limit = min(settings.max_fanout, parent.config.max_fanout)
        if parent.spawned >= fanout_limit:
            return ToolResult(ok=False, content=f"refused: maximum fan-out reached ({fanout_limit} subagents)")
        try:
            if args.get("agent"):
                inline = dict(args["agent"])
                base = template(str(args.get("template") or "coder"))
                child = base.model_copy(update={
                    "name": str(inline.get("name") or base.name)[:80],
                    "system_prompt": str(inline.get("system_prompt") or base.system_prompt),
                    "tool_permissions": [p for p in inline.get("tool_permissions", base.tool_permissions)
                                         if p in ctx.agent.tool_permissions],
                    "template": None,
                })
                child = AgentConfig.model_validate(child.model_dump())
            else:
                child = template(str(args.get("template") or "coder"))
        except (KeyError, ValueError) as exc:
            return ToolResult(ok=False, content=f"error: invalid subagent config: {exc}")
        child = child.narrowed(parent.config)
        key = task_key(child, task)
        if key == parent.key or any(a.key == key for a in parent.ancestors()):
            return ToolResult(ok=False, content="refused: cycle detected — an ancestor is already working on "
                                                "this exact task")
        parent.spawned += 1  # reserve the slot before the first await
        result = await runtime.run_agent(rc, child, task, role=child.template or child.name, parent=parent,
                                         context=str(args.get("context") or ""))
        summary = f"subagent '{child.name}' finished with status {result.status}"
        if result.files_changed:
            summary += f"; files changed: {', '.join(result.files_changed[:20])}"
        return ToolResult(ok=result.status == "done", content=f"{summary}\n\n{result.output[:6000]}",
                          evidence=result.evidence, files_changed=result.files_changed,
                          data={"call_id": result.call_id, "status": result.status, "model": result.model})
