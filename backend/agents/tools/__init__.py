"""Built-in tool registry."""

from __future__ import annotations

from backend.agents.tools.base import ToolRegistry
from backend.agents.tools.fs import FS_TOOLS
from backend.agents.tools.terminal import TERMINAL_TOOLS


def build_registry() -> ToolRegistry:
    from backend.agents.runtime import SpawnSubagent

    registry = ToolRegistry()
    for tool in [*FS_TOOLS, *TERMINAL_TOOLS, SpawnSubagent()]:
        registry.register(tool)
    return registry
