"""A deterministic runner used to test the executor/events/vault without LLMs."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.orchestrator.executor import RunHandle


class FakeRunner:
    def __init__(self, delay: float = 0.0, fail: bool = False, block: asyncio.Event | None = None) -> None:
        self.delay = delay
        self.fail = fail
        self.block = block
        self.started: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.order: list[str] = []

    async def __call__(self, handle: RunHandle) -> None:
        rec = handle.recorder
        self.started.append(handle.run_id)
        self.order.append(handle.user_id)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await rec.set_status("planning")
            root = await rec.start_call(role="planner", agent_name="planner", task="plan it", depth=0,
                                        category="heavy_reasoning")
            await rec.set_call_model(root, "mock-pro", "prv_x")
            child = await rec.start_call(role="coder", agent_name="coder", task="code it", depth=1,
                                         parent_call_id=root, category="code_generation", lineage=[root])
            await rec.set_call_model(child, "mock-mini", "prv_x")
            rec.emit("token.delta", {"text": "hello "}, call_id=child)
            rec.emit("token.delta", {"text": "world"}, call_id=child)
            if self.block is not None:
                await self.block.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            await rec.add_call_usage(child, 42, 0.001)
            await rec.finish_call(child, status="done", output="def hello(): return 'world'")
            await rec.record_decision(type="verification", engine="rules", choice="confirm_done", confidence=0.91,
                                      alternatives={"confirm_done": 0.91, "restart_same_agent": 0.09},
                                      latency_ms=1.5, input_state={"tests": "1 passed"}, call_id=child)
            await rec.finish_call(root, status="done", output="plan complete")
            await rec.upsert_diff("--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+print('hi')\n",
                                  {"files_changed": 1, "additions": 1, "deletions": 0})
            if self.fail:
                raise RuntimeError("scripted failure")
            await rec.add_usage(42, 0.001)
            await rec.set_status("done", final_output="Hello world ✓ — done", iteration_count=1)
        finally:
            self.concurrent -= 1


def install(ctx: Any, runner: FakeRunner) -> FakeRunner:
    ctx.executor.runner = runner
    return runner
