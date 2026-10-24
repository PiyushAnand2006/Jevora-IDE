"""LangGraph orchestrator: a state graph (not a fixed pipeline) for Build, Chat, Research and Analysis.

    classify ─┬─ planner ─→ coder | ui_designer | custom_agent ─→ tester ─→ critic ─→ verifier ─┬→ finalize
              ├─ chat ─────────────────────────────(changes?)──→ tester … verifier ────────────┤
              ├─ researcher ─→ verifier                                                       ├→ (retry) worker
              ├─ analyst ────→ verifier                                                       └→ escalate ─→ …
              └─ run_command / critic(/review) / planner(/plan) / tester(/test) ───────────────→ finalize

Conditional edges look at state: did tests pass, is confidence above threshold, is user input needed,
was the iteration ceiling hit. State is checkpointed in PostgreSQL for deployed environments
(SQLite in tests), so runs survive a
restart and resume after an escalation answer via ``interrupt``/``Command(resume=...)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from backend.agents.pool import RunBudget
from backend.agents.runtime import RunContext
from backend.agents.workspace_state import WorkspaceSnapshot, load_snapshot, save_snapshot
from backend.conversations import owned_conversation
from backend.core.logging import get_logger
from backend.db.models import Run, Workspace
from backend.orchestrator.executor import RunHandle
from backend.orchestrator.nodes.analyst import analyst
from backend.orchestrator.nodes.chat import chat
from backend.orchestrator.nodes.classify import classify
from backend.orchestrator.nodes.coder import coder, custom_agent
from backend.orchestrator.nodes.critic import critic
from backend.orchestrator.nodes.finalize import SUCCESS, finalize, run_command
from backend.orchestrator.nodes.planner import planner
from backend.orchestrator.nodes.researcher import researcher
from backend.orchestrator.nodes.tester import tester
from backend.orchestrator.nodes.ui_designer import ui_designer
from backend.orchestrator.nodes.verifier import escalate, verifier
from backend.orchestrator.state import GraphState
from backend.providers.base import NoProviderConfigured

if TYPE_CHECKING:
    from backend.app_context import AppContext

log = get_logger(__name__)
NodeFn = Callable[["Orchestrator", GraphState], Awaitable[dict[str, Any]]]

NODES: dict[str, NodeFn] = {
    "classify": classify, "planner": planner, "coder": coder, "ui_designer": ui_designer,
    "custom_agent": custom_agent, "chat": chat, "researcher": researcher, "analyst": analyst, "tester": tester,
    "critic": critic, "verifier": verifier, "escalate": escalate, "run_command": run_command, "finalize": finalize,
}
BUILD_WORKERS = ("coder", "ui_designer", "custom_agent")


def _worker_for(state: GraphState) -> str:
    mode = state["mode"]
    if mode == "research":
        return "researcher"
    if mode == "analysis":
        return "analyst"
    if mode == "chat":
        return "chat"
    node = state.get("worker_node") or "coder"
    return node if node in BUILD_WORKERS else "coder"


def route_classify(state: GraphState) -> str:
    if state.get("final_status") == "failed":
        return "finalize"
    command = state.get("command")
    by_command = {"/run": "run_command", "/review": "critic", "/plan": "planner", "/test": "tester"}
    if command in by_command:
        return by_command[command]
    mode = state["mode"]
    if mode == "build":
        return "planner"
    return _worker_for(state)


def route_planner(state: GraphState) -> str:
    return "finalize" if state.get("command") == "/plan" else _worker_for(state)


def route_build_worker(state: GraphState) -> str:
    return "tester"


def route_chat(state: GraphState) -> str:
    if state.get("files_changed") and state.get("flags", {}).get("decision_verification", True):
        return "tester"
    return "finalize"


def route_tester(state: GraphState) -> str:
    return "verifier" if state.get("command") == "/test" else "critic"


def route_critic(state: GraphState) -> str:
    if state.get("command") == "/review":
        return "finalize"
    return "verifier" if state.get("flags", {}).get("decision_verification", True) else "finalize"


def route_report_worker(state: GraphState) -> str:
    return "verifier" if state.get("flags", {}).get("decision_verification", True) else "finalize"


def route_verifier(state: GraphState) -> str:
    verification = state.get("verification") or {}
    if verification.get("choice") in SUCCESS:
        return "finalize"
    escalation = state.get("escalation")
    if escalation and not escalation.get("choice"):
        return "escalate"
    if int(state.get("iteration", 0)) >= int(state.get("max_iterations", 6)):
        return "escalate"
    if state.get("command") == "/test":
        return "finalize"
    return _worker_for(state)


def route_escalate(state: GraphState) -> str:
    choice = (state.get("escalation") or {}).get("choice")
    if choice in ("retry", "reassign"):
        return _worker_for(state)
    return "finalize"


class Orchestrator:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self._contexts: dict[str, RunContext] = {}
        self._saver: Any = None
        self._saver_cm: AbstractAsyncContextManager[Any] | None = None
        self.graph: Any = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        database_url = self.app.settings.resolved_database_url
        if database_url.startswith("postgresql+asyncpg://"):
            # LangGraph's Postgres saver uses psycopg's async connection API,
            # while the application ORM uses asyncpg through SQLAlchemy.
            checkpoint_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
            self._saver_cm = AsyncPostgresSaver.from_conn_string(checkpoint_url)
        else:
            path = self.app.settings.checkpoint_db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            self._saver_cm = AsyncSqliteSaver.from_conn_string(str(path))
        saver_cm = self._saver_cm
        assert saver_cm is not None
        self._saver = await saver_cm.__aenter__()
        if isinstance(self._saver, AsyncPostgresSaver):
            await self._saver.setup()
        self.graph = self.build().compile(checkpointer=self._saver)

    async def stop(self) -> None:
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(None, None, None)
            self._saver_cm = None

    def build(self) -> StateGraph[GraphState]:
        graph: StateGraph[GraphState] = StateGraph(GraphState)
        for name, fn in NODES.items():
            graph.add_node(name, cast(Any, self._wrap(name, fn)))
        graph.add_edge(START, "classify")
        workers = ["coder", "ui_designer", "custom_agent", "chat", "researcher", "analyst"]
        graph.add_conditional_edges("classify", route_classify,
                                    ["planner", "run_command", "critic", "tester", "finalize", *workers])
        graph.add_conditional_edges("planner", route_planner, ["finalize", *workers])
        for worker in BUILD_WORKERS:
            graph.add_conditional_edges(worker, route_build_worker, ["tester"])
        graph.add_conditional_edges("chat", route_chat, ["tester", "finalize"])
        graph.add_conditional_edges("researcher", route_report_worker, ["verifier", "finalize"])
        graph.add_conditional_edges("analyst", route_report_worker, ["verifier", "finalize"])
        graph.add_conditional_edges("tester", route_tester, ["verifier", "critic"])
        graph.add_conditional_edges("critic", route_critic, ["verifier", "finalize"])
        graph.add_conditional_edges("verifier", route_verifier, ["finalize", "escalate", *workers])
        graph.add_conditional_edges("escalate", route_escalate, ["finalize", *workers])
        graph.add_edge("run_command", "finalize")
        graph.add_edge("finalize", END)
        return graph

    def _wrap(self, name: str, fn: NodeFn) -> Callable[[GraphState], Awaitable[dict[str, Any]]]:
        async def node(state: GraphState) -> dict[str, Any]:
            rc = self.context(state["run_id"])
            if rc.scope.cancelled:
                raise asyncio.CancelledError
            await self.app.plugins.run_hooks("before_node", rc.user_id, node=name, run_id=rc.run_id, state=state)
            updates = await fn(self, state)
            await self.app.plugins.run_hooks("after_node", rc.user_id, node=name, run_id=rc.run_id, updates=updates)
            return updates

        node.__name__ = f"node_{name}"
        return node

    # ------------------------------------------------------------------ contexts
    def context(self, run_id: str) -> RunContext:
        return self._contexts[run_id]

    def _snapshot_path(self, run_id: str) -> Path:
        return self.app.settings.data_dir / "snapshots" / f"{run_id}.json.gz"

    async def _workspace_for(self, run: Run) -> Path:
        if run.workspace_id:
            async with self.app.sessions() as session:
                ws = await session.get(Workspace, run.workspace_id)
            if ws is not None and ws.user_id == run.user_id and await asyncio.to_thread(Path(ws.root_path).exists):
                return Path(ws.root_path)
        return self.app.settings.workspace_root

    async def _build_context(self, run: Run, handle: RunHandle, fresh: bool) -> RunContext:
        s = self.app.settings
        scope = self.app.agent_pool.scope(run.id, run.user_id,
                                          RunBudget(s.run_token_budget, s.run_cost_budget_usd, s.run_time_budget_s))
        workspace = await self._workspace_for(run)
        snapshot_path = self._snapshot_path(run.id)
        snapshot: WorkspaceSnapshot | None = None if fresh else await asyncio.to_thread(load_snapshot, snapshot_path)
        if snapshot is None:
            snapshot = await asyncio.to_thread(WorkspaceSnapshot.capture, workspace)
            await asyncio.to_thread(save_snapshot, snapshot, snapshot_path)
        flags = await self.app.plugins.flags(run.user_id)

        async def route_fn(task: str, agent_name: str) -> str:
            outcome = await self.app.decisions.route_model({"task": task[:2000], "mode": run.mode,
                                                            "agent": agent_name}, recorder=handle.recorder)
            return outcome.result.choice

        rc = RunContext(app=self.app, run_id=run.id, user_id=run.user_id, mode=run.mode, task=run.task,
                        recorder=handle.recorder, scope=scope, workspace=workspace,
                        model_override=run.model_override, snapshot=snapshot, flags=flags, route_fn=route_fn)
        self._contexts[run.id] = rc
        return rc

    def _initial_state(self, run: Run, flags: dict[str, bool]) -> GraphState:
        return GraphState(
            run_id=run.id, user_id=run.user_id, mode=run.mode, task=run.task, command=run.command,
            command_arg=run.task, agent_id=run.agent_id, model_override=run.model_override,
            conversation_id=run.conversation_id, attachments=list(run.attachments or []),
            web_search=bool((run.meta or {}).get("web_search", False) or run.mode == "research"), flags=flags,
            iteration=0, max_iterations=self.app.settings.iteration_ceiling, same_agent_failures=0,
            worker_attempts=0, evidence=[], files_changed=[], escalation=None)

    # ------------------------------------------------------------------ the executor's runner
    async def run(self, handle: RunHandle) -> None:
        async with self.app.sessions() as session:
            run = await session.get(Run, handle.run_id)
            if run is not None and run.conversation_id is not None:
                await owned_conversation(session, run.user_id, run.conversation_id, run.mode)
        if run is None:
            return
        config: dict[str, Any] = {"configurable": {"thread_id": run.id}, "recursion_limit": 500}
        checkpoint = await self.graph.aget_state(config)
        has_checkpoint = bool(checkpoint.values)
        fresh = handle.resume is None or not has_checkpoint
        rc = await self._build_context(run, handle, fresh)
        if not fresh:
            # Calls that were running when the process stopped will never finish; close them out.
            await handle.recorder.cancel_open_calls("interrupted (resumed from checkpoint)")
        payload: Any
        if fresh:
            payload = self._initial_state(run, rc.flags)
        elif isinstance(handle.resume, dict) and self._interrupted(checkpoint):
            payload = Command(resume=handle.resume)
        else:
            payload = None  # continue from the last checkpoint
        await self.app.plugins.run_hooks("before_run", run.user_id, run_id=run.id, resumed=not fresh)
        try:
            await self.graph.ainvoke(payload, config)
        except NoProviderConfigured as exc:
            await handle.recorder.set_status("failed", error=str(exc), final_output=f"Run failed: {exc}")
        finally:
            self._contexts.pop(run.id, None)
            await self.app.plugins.run_hooks("after_run", run.user_id, run_id=run.id)
        final = await self.graph.aget_state(config)
        if not self._interrupted(final) and not final.next:
            async with self.app.sessions() as session:
                current = await session.get(Run, run.id)
            if current is not None and current.status not in ("done", "failed", "escalated"):
                await handle.recorder.set_status("failed", error="orchestrator ended without a result")

    @staticmethod
    def _interrupted(snapshot: Any) -> bool:
        if getattr(snapshot, "interrupts", None):
            return True
        return any(getattr(task, "interrupts", None) for task in getattr(snapshot, "tasks", ()) or ())

    async def forget(self, run_id: str) -> None:
        if self._saver is not None:
            try:
                await self._saver.adelete_thread(run_id)
            except Exception:
                log.exception("could not delete checkpoints for %s", run_id)
        self._snapshot_path(run_id).unlink(missing_ok=True)
