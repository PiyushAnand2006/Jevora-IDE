from __future__ import annotations

import pytest

from backend import main
from backend.schemas import AgentConfig, TaskRun, TaskSpec
from backend.storage import write_run_note
from backend import workspace_tools
from backend.workspace_tools import command_is_safe, run_agent_command, run_command


@pytest.mark.asyncio
async def test_research_agent_without_terminal_permission_cannot_run_shell():
    result = await run_agent_command(["workspace_read"], "echo should-not-run")

    assert result["exit_code"] == -1
    assert "not permitted" in result["stderr"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["rm -rf /tmp/example", "cat ~/.ssh/id_rsa", "type $HOME\\.ssh\\id_rsa"])
async def test_destructive_and_sensitive_commands_are_blocked(command):
    assert not command_is_safe(command)
    result = await run_command(command)
    assert result == {"exit_code": -1, "stdout": "", "stderr": "Command blocked by workspace sandbox"}


def test_workspace_paths_cannot_escape_root_and_file_helpers_stay_inside(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workspace_tools, "WORKSPACE_DIR", tmp_path)

    workspace_tools.write_file("src/example.txt", "hello")

    assert workspace_tools.read_file("src/example.txt") == "hello"
    assert workspace_tools.list_files() == ["src/example.txt"]
    with pytest.raises(ValueError):
        workspace_tools.resolve_path("../../outside.txt")


@pytest.mark.asyncio
async def test_api_keys_are_redacted_from_events_and_vault_notes(vault_dir):
    run = TaskRun(spec=TaskSpec(text="api_key=sk-test-event-secret"))
    await main.emit(run, "agent_token", {"text": "Authorization: Bearer sk-test-event-secret"})
    run.final_output = "nvapi-test-event-secret"
    note = write_run_note(run.model_dump(mode="json"))
    event_text = str(main.EVENTS[run.id])
    note_text = note.read_text(encoding="utf-8")

    assert "sk-test-event-secret" not in event_text
    assert "nvapi-test-event-secret" not in note_text
    assert "[REDACTED]" in event_text
