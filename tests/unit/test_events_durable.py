"""Durable event replay: events survive a bus restart (events table)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.events import EventBus
from backend.db.base import Base


@pytest.fixture
def db(tmp_path: Path):
    async def make():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ev.db'}", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False), engine
    return asyncio.run(make())


def test_events_survive_bus_restart(db) -> None:
    sessions, engine = db

    async def scenario():
        bus1 = EventBus()
        bus1.attach_persistence(sessions)
        bus1.publish("run.created", user_id="u1", run_id="r1", data={"mode": "build"})
        bus1.publish("token.delta", user_id="u1", run_id="r1", data={"text": "hi"})  # skipped: not persisted
        bus1.publish("agent.started", user_id="u1", run_id="r1", data={"name": "coder"})
        await bus1.close()  # final flush

        # a "restarted" process: fresh bus over the same DB
        bus2 = EventBus()
        bus2.attach_persistence(sessions)
        await bus2.restore()

        # the seq counter continued from the persisted maximum
        event = bus2.publish("agent.finished", user_id="u1", run_id="r1", data={})
        assert event.seq == 4  # 1,2,3 persisted; token.delta skipped but still consumes seq

        history = await bus2.history_durable("r1", 0)
        types = [e.type for e in history]
        assert types == ["run.created", "agent.started", "agent.finished"]  # token.delta not replayed
        assert all(e.seq > 0 for e in history)

        # incremental replay from a client's last_seq
        tail = await bus2.history_durable("r1", 3)
        assert [e.type for e in tail] == ["agent.finished"]

        # forget removes durable rows too
        await bus2.forget_run_persisted("r1")
        assert await bus2.history_durable("r1", 0) == []
        await bus2.close()

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_unpersisted_bus_unchanged(tmp_path: Path) -> None:
    """No persistence attached -> history() behaves exactly as before."""
    async def scenario():
        bus = EventBus()
        bus.publish("run.created", run_id="r2")
        assert [e.type for e in bus.history("r2", 0)] == ["run.created"]
        assert await bus.history_durable("r2", 0) == bus.history("r2", 0)

    asyncio.run(scenario())
