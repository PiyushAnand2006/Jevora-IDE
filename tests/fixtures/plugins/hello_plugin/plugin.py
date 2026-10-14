"""Example third-party plugin: adds a `hello` tool and records run hooks."""

from __future__ import annotations

from typing import Any

from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema

EVENTS: list[str] = []


class HelloTool(Tool):
    name = "hello"
    description = "Say hello to someone."
    permission = "fs.read"
    parameters = schema({"name": {"type": "string"}}, ["name"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(content=f"hello, {args['name']}!")


def register(api: Any) -> None:
    api.add_tool(HelloTool())

    async def before_run(**payload: Any) -> None:
        EVENTS.append(f"before_run:{payload['run_id']}")

    async def after_node(**payload: Any) -> None:
        EVENTS.append(f"after_node:{payload['node']}")

    async def on_decision(**payload: Any) -> None:
        EVENTS.append(f"decision:{payload['decision']['type']}:{payload['decision']['choice']}")

    api.add_hook("before_run", before_run)
    api.add_hook("after_node", after_node)
    api.add_hook("on_decision", on_decision)
