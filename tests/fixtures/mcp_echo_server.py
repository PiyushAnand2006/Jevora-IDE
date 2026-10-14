"""A tiny MCP stdio server used by the integration tests."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("jevora-test-echo")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back, upper-cased."""
    return text.upper()


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run("stdio")
