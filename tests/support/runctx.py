"""Helpers to build a RunContext against a live AppContext without the orchestrator."""

from __future__ import annotations

from typing import Any

from backend.agents.pool import RunBudget
from backend.agents.runtime import RunContext
from backend.agents.workspace_state import WorkspaceSnapshot
from backend.core.ids import new_id
from backend.db.models import Run
from backend.orchestrator.recorder import RunRecorder


async def make_run_context(ctx: Any, task: str = "test task", mode: str = "build", **kwargs: Any) -> RunContext:
    run_id = new_id("run_")
    async with ctx.sessions() as session:
        session.add(Run(id=run_id, user_id=ctx.local_user_id, mode=mode, task=task, status="running"))
        await session.commit()
    recorder = RunRecorder(ctx.sessions, ctx.events, run_id, ctx.local_user_id, ctx.metrics)
    s = ctx.settings
    scope = ctx.agent_pool.scope(run_id, ctx.local_user_id,
                                 kwargs.pop("budget", None) or RunBudget(s.run_token_budget, s.run_cost_budget_usd,
                                                                         s.run_time_budget_s))
    return RunContext(app=ctx, run_id=run_id, user_id=ctx.local_user_id, mode=mode, task=task, recorder=recorder,
                      scope=scope, workspace=s.workspace_root,
                      snapshot=WorkspaceSnapshot.capture(s.workspace_root), **kwargs)
