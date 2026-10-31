"""Per-workspace run serialization: runs sharing a workspace never overlap.

Concurrent runs in one workspace used to see each other's changes in their
per-run diffs; the executor now grants a workspace to one run at a time while
different workspaces still run in parallel.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.events import EventBus
from backend.db.base import Base
from backend.db.models import Run, User, Workspace
from backend.orchestrator.executor import RunExecutor


@pytest.fixture
def db(tmp_path: Path):
    async def make():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ex.db'}", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(User(id="u1", email="u@x.local", name="U"))
            session.add(Workspace(id="wsA", user_id="u1", name="A", root_path="a"))
            session.add(Workspace(id="wsB", user_id="u1", name="B", root_path="b"))
            session.add(Run(id="r1", user_id="u1", workspace_id="wsA", mode="build", task="t1"))
            session.add(Run(id="r2", user_id="u1", workspace_id="wsA", mode="build", task="t2"))
            session.add(Run(id="r3", user_id="u1", workspace_id="wsB", mode="build", task="t3"))
            await session.commit()
        return sessions, engine
    return asyncio.run(make())


def test_same_workspace_serializes_different_workspace_parallel(db) -> None:
    sessions, engine = db

    async def scenario():
        executor = RunExecutor(sessions, EventBus(), max_concurrent=4)
        spans: dict[str, tuple[float, float]] = {}

        async def runner(handle) -> None:
            start = time.monotonic()
            await asyncio.sleep(0.25)
            spans[handle.run_id] = (start, time.monotonic())

        executor.runner = runner
        await executor.start()
        executor.submit("r1", "u1")
        executor.submit("r2", "u1")
        executor.submit("r3", "u1")
        deadline = asyncio.get_running_loop().time() + 10
        while len(spans) < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        await executor.stop()
        return spans

    spans = asyncio.run(scenario())
    assert set(spans) == {"r1", "r2", "r3"}
    s1, e1 = spans["r1"]
    s2, _ = spans["r2"]
    s3, _ = spans["r3"]
    # same workspace: no overlap regardless of dispatch order
    first_end = min(e1, spans["r2"][1])
    assert max(s1, s2) >= first_end - 0.05, "runs in one workspace must not overlap"
    # different workspace: r3 started while wsA was still busy (no cross-workspace blocking)
    assert s3 < max(e1, spans["r2"][1]), "wsB run should have started while wsA was busy"

    asyncio.run(engine.dispose())
