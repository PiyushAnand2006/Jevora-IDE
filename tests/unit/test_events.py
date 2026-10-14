from __future__ import annotations

import asyncio

from backend.core.events import EventBus, replay_then_follow


async def test_publish_and_history_replay() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    e1 = bus.publish("run.status", user_id="u", run_id="r1", data={"status": "queued"})
    bus.publish("run.status", user_id="u", run_id="r2", data={"status": "queued"})
    e3 = bus.publish("token.delta", user_id="u", run_id="r1", data={"text": "x"})
    assert e3.seq > e1.seq
    assert [e.seq for e in bus.history("r1")] == [e1.seq, e3.seq]
    assert [e.seq for e in bus.history("r1", after_seq=e1.seq)] == [e3.seq]
    # token deltas are excluded from the global replay buffer
    assert all(e.type != "token.delta" for e in bus.global_history("u"))


async def test_replay_then_follow_dedupes() -> None:
    bus = EventBus()
    sub = bus.subscribe(run_id="r")
    first = bus.publish("agent.started", run_id="r")
    backlog = bus.history("r", 0)
    second = bus.publish("agent.finished", run_id="r")
    seen: list[int] = []

    async def consume() -> None:
        async for event in replay_then_follow(bus, sub, backlog):
            seen.append(event.seq)
            if len(seen) == 2:
                return

    await asyncio.wait_for(consume(), timeout=2)
    assert seen == [first.seq, second.seq]
    sub.close()


async def test_user_filter_for_global_stream() -> None:
    bus = EventBus()
    mine = bus.subscribe(user_id="alice")
    bus.publish("run.status", user_id="bob", run_id="x")
    event = bus.publish("run.status", user_id="alice", run_id="y")
    got = await asyncio.wait_for(mine.queue.get(), timeout=1)
    assert got is not None and got.seq == event.seq
    mine.close()


async def test_threadsafe_publish() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe(run_id="t")
    await asyncio.to_thread(bus.publish_threadsafe, "terminal.output", run_id="t", data={"data": "hi"})
    got = await asyncio.wait_for(sub.queue.get(), timeout=2)
    assert got is not None and got.data == {"data": "hi"}
