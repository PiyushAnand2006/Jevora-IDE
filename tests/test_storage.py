from __future__ import annotations

from backend.schemas import AgentConfig, AgentRun, Decision, TaskRun, TaskSpec
from backend.storage import write_run_note


def test_vault_note_contains_tree_models_decisions_and_timestamps(vault_dir):
    run = TaskRun(spec=TaskSpec(text="Build a feature"))
    parent = AgentRun(config=AgentConfig(name="Planner"), model_used="planner-model", status="done", finished_at=20.0)
    child = AgentRun(parent_id=parent.id, config=AgentConfig(name="Coder"), model_used="coder-model", status="done", finished_at=30.0)
    run.agents = [parent, child]
    run.decisions = [Decision(kind="work_verification", choice="confirm_done", confidence=0.91, engine="stub")]
    run.status = "done"
    run.final_output = "api_key=sk-test-this-must-not-appear"

    note = write_run_note(run.model_dump(mode="json"))
    text = note.read_text(encoding="utf-8")

    assert note.is_relative_to(vault_dir)
    assert f"`{parent.id}` → `{child.id}`" in text
    assert "planner-model" in text and "coder-model" in text
    assert "confirm_done" in text and "91%" in text
    assert "started:" in text and "finished:" in text and "created_at:" in text
    assert "sk-test-this-must-not-appear" not in text
    assert "[REDACTED]" in text
