"""@-mentions: run creation stores them on the run for the graph to inline."""

import pytest


@pytest.mark.asyncio
async def test_run_stores_mentions_in_meta(harness) -> None:
    client = harness.client
    created = (await client.post("/api/runs", json={
        "text": "fix the bug in @src/main.py and check @docs/notes.md",
        "mode": "build",
        "mentions": ["src/main.py", "docs/notes.md", "../outside.txt"],
    })).json()
    assert created["meta"]["mentions"] == ["src/main.py", "docs/notes.md", "../outside.txt"]
