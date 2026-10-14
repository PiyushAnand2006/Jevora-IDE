from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.orchestrator.records import CallRecord, DecisionRecord, RunRecord, build_tree
from backend.vault.obsidian import Vault, VaultError
from backend.vault.templates import safe_filename


def sample_record() -> RunRecord:
    return RunRecord(
        run_id="run_abc", user_id="usr_local", mode="build", task="Create a Flask hello-world: with tests?",
        status="done", created_at="2026-10-02T10:00:00Z", started_at="2026-10-02T10:00:01Z",
        finished_at="2026-10-02T10:00:09Z", final_output="All good.\n```python\nprint('x')\n```",
        iteration_count=2, total_tokens=900, cost_estimate=0.0123,
        calls=[
            CallRecord(call_id="call_1", role="planner", agent_name="planner", model_used="mock-pro", depth=0,
                       status="done", started_at="2026-10-02T10:00:01Z", ended_at="2026-10-02T10:00:03Z",
                       tokens=300, input="plan", output="1. write app\n2. test"),
            CallRecord(call_id="call_2", parent_call_id="call_1", role="coder", agent_name="coder",
                       model_used="mock-mini", depth=1, status="done", started_at="2026-10-02T10:00:03Z",
                       ended_at="2026-10-02T10:00:08Z", tokens=600, input="code", output="wrote app.py",
                       lineage=["call_1"]),
            CallRecord(call_id="call_3", parent_call_id="call_2", role="tester", agent_name="tester",
                       model_used=None, depth=2, status="done", started_at="2026-10-02T10:00:05Z",
                       ended_at="2026-10-02T10:00:06Z"),
        ],
        decisions=[
            DecisionRecord(id="dec_1", type="routing", engine="rules", choice="code_generation", confidence=0.8,
                           created_at="2026-10-02T10:00:01Z"),
            DecisionRecord(id="dec_2", call_id="call_2", type="verification", engine="rules",
                           choice="confirm_done", confidence=0.92, created_at="2026-10-02T10:00:08Z",
                           alternatives={"confirm_done": 0.92, "escalate": 0.08}),
            DecisionRecord(id="dec_3", call_id="call_2", type="verification", engine="laya",
                           choice="restart_same_agent", confidence=0.4, shadow=True, acted=False,
                           created_at="2026-10-02T10:00:08Z"),
        ],
        diff="--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+print('hi')\n",
    )


def snapshot(vault_root: Path) -> dict[str, str]:
    return {p.relative_to(vault_root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(vault_root.rglob("*.md"))}


def test_write_is_idempotent_and_round_trips(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    record = sample_record()
    note = vault.write_run(record)
    assert note.exists() and "(run_abc)" in note.name
    first = snapshot(vault.path)
    vault.write_run(record)
    assert snapshot(vault.path) == first  # byte-identical, no duplicated list lines

    read = vault.read_run("run_abc")
    assert read is not None
    assert build_tree(read) == build_tree(record)
    assert read.decisions == record.decisions and read.diff == record.diff

    content = note.read_text(encoding="utf-8")
    assert "[[Jevora/Calls/run_abc/call_2|coder · mock-mini]]" in content
    call_note = (vault.root / "Calls" / "run_abc" / "call_2.md").read_text(encoding="utf-8")
    assert "[[Jevora/Calls/run_abc/call_1|planner · mock-pro]]" in call_note  # called by
    assert "[[Jevora/Calls/run_abc/call_3|tester · no model]]" in call_note  # subagent
    index = (vault.root / "Jevora Index.md").read_text(encoding="utf-8")
    assert index.count("<!-- key:run_abc -->") == 1
    assert (vault.root / "Daily" / "2026-10-02.md").exists()
    assert (vault.root / "Agents" / "coder.md").exists()


def test_user_edits_are_preserved(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    record = sample_record()
    note = vault.write_run(record)
    edited = note.read_text(encoding="utf-8").replace(
        "_Your notes below are preserved when Jevora updates this note._",
        "_Your notes below are preserved when Jevora updates this note._\nMy own analysis of this run.")
    edited = edited.replace("tags:", "reviewed: true\ntags:", 1)
    note.write_text(edited, encoding="utf-8")
    record.status = "escalated"
    record.final_output = "updated output"
    vault.write_run(record)
    content = note.read_text(encoding="utf-8")
    assert "My own analysis of this run." in content
    assert "reviewed: true" in content
    assert "updated output" in content and "status: escalated" in content


def test_missing_managed_section_is_reappended(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    note = vault.write_run(sample_record())
    text = note.read_text(encoding="utf-8")
    start = text.index("<!-- jevora:begin diff -->")
    end = text.index("<!-- jevora:end diff -->") + len("<!-- jevora:end diff -->")
    note.write_text(text[:start] + text[end:], encoding="utf-8")
    vault.write_run(sample_record())
    assert "<!-- jevora:begin diff -->" in note.read_text(encoding="utf-8")


def test_nonexistent_vault_is_created(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "does" / "not" / "exist")
    assert vault.check_writable() == (True, None)
    assert vault.write_run(sample_record()).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_unwritable_vault_raises_vault_error(tmp_path: Path) -> None:  # pragma: no cover - posix only
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    vault = Vault(locked)
    assert vault.check_writable()[0] is False
    with pytest.raises(VaultError):
        vault.write_run(sample_record())


def test_unwritable_vault_path_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    vault = Vault(blocker)
    ok, error = vault.check_writable()
    assert ok is False and error
    with pytest.raises(VaultError):
        vault.write_run(sample_record())


def test_delete_run_removes_generated_notes(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.write_run(sample_record())
    assert vault.delete_run("run_abc") == 4
    assert vault.find_run_note("run_abc") is None
    assert "run_abc" not in (vault.root / "Jevora Index.md").read_text(encoding="utf-8")


def test_safe_filenames() -> None:
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j#k^[l]') == "a b c d e f g h i j k l"
    assert safe_filename("   ") == "untitled"
    assert len(safe_filename("x" * 500)) == 80
