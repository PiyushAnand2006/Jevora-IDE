"""Plan parsing, snapshot restore, and the auto-commit helper."""

import subprocess
from pathlib import Path

import pytest

from backend.agents.workspace_state import (
    WorkspaceSnapshot,
    load_snapshot,
    restore_snapshot,
    save_snapshot,
)
from backend.orchestrator.autocommit import commit_run_changes
from backend.orchestrator.nodes.planner import parse_plan_items


# --------------------------------------------------------------------------- plan items
def test_parse_plan_items_numbered_and_bulleted() -> None:
    plan = (
        "Here is the plan:\n"
        "1. Set up the database schema\n"
        "2. [x] Add the API route\n"
        "- Write tests for the route\n"
        "* Polish the UI\n"
        "\n"
        "Acceptance criteria: it works\n"
    )
    assert parse_plan_items(plan) == [
        "Set up the database schema",
        "Add the API route",
        "Write tests for the route",
        "Polish the UI",
    ]


def test_parse_plan_items_fallback_and_limit() -> None:
    assert parse_plan_items("") == []
    assert parse_plan_items("Just do the thing.") == ["Just do the thing."]
    items = parse_plan_items("\n".join(f"{i}. step {i}" for i in range(1, 30)), limit=5)
    assert len(items) == 5 and items[0] == "step 1"


# --------------------------------------------------------------------------- restore
def test_restore_snapshot_roundtrip(tmp_path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "keep.py").write_text("original\n", encoding="utf-8")
    (ws / "src" / "gone.txt").write_text("delete me\n", encoding="utf-8")

    snap = WorkspaceSnapshot.capture(ws)
    save_snapshot(snap, tmp_path / "snap.json.gz")
    loaded = load_snapshot(tmp_path / "snap.json.gz")
    assert loaded is not None

    # Simulate a run: modify one file, delete another, create one.
    # (Ignored dirs such as .venv/node_modules are structurally out of reach:
    # capture() never records them, so restore() can never touch them.)
    (ws / "src" / "keep.py").write_text("changed\n", encoding="utf-8")
    (ws / "src" / "gone.txt").unlink()
    (ws / "new_file.py").write_text("added by the run\n", encoding="utf-8")

    report = restore_snapshot(loaded)
    assert (ws / "src" / "keep.py").read_text(encoding="utf-8") == "original\n"
    assert (ws / "src" / "gone.txt").read_text(encoding="utf-8") == "delete me\n"
    assert not (ws / "new_file.py").exists()
    assert set(report["restored"]) == {"src/keep.py", "src/gone.txt"}
    assert report["deleted"] == ["new_file.py"]


def test_restore_snapshot_reports_binary_skips(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x01" * 32)
    snap = WorkspaceSnapshot.capture(ws)
    (ws / "img.png").write_bytes(b"\x00\x02" * 32)  # binary change (content not captured)
    report = restore_snapshot(snap)
    assert report["skipped"] == ["img.png"] and not report["restored"]


# --------------------------------------------------------------------------- auto-commit
def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout


@pytest.mark.asyncio
async def test_commit_run_changes_commits_only_run_files(tmp_path) -> None:
    ws = tmp_path / "repo"
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "config", "user.email", "human@example.com")
    _git(ws, "config", "user.name", "Human")
    (ws / "base.txt").write_text("base\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")

    # The run changes two files; the user has an unrelated dirty file.
    (ws / "base.txt").write_text("changed by run\n", encoding="utf-8")
    (ws / "feature.py").write_text("new\n", encoding="utf-8")
    (ws / "unrelated.txt").write_text("human wip\n", encoding="utf-8")

    commit = await commit_run_changes(ws, task="Build the feature\nmore detail",
                                      run_id="run_1234567890", files=["base.txt", "feature.py"],
                                      deleted=[])
    assert commit is not None and commit["files"] == 2
    assert "Build the feature" in commit["message"] and "run_1234567890"[-8:] in commit["message"]
    log = _git(ws, "log", "-1", "--name-only", "--format=%an|%s")
    assert "Jevora Agent" in log.splitlines()[0] or "Jevora Agent" in _git(ws, "log", "-1", "--format=%an")
    assert "feature.py" in log and "base.txt" in log and "unrelated.txt" not in log


@pytest.mark.asyncio
async def test_commit_run_changes_skips_clean_and_non_repo(tmp_path) -> None:
    ws = tmp_path / "norepo"
    ws.mkdir()
    (ws / "a.txt").write_text("x\n", encoding="utf-8")
    assert await commit_run_changes(ws, task="t", run_id="run_1", files=["a.txt"], deleted=[]) is None

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "h@e.com")
    _git(repo, "config", "user.name", "H")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init", "--allow-empty")
    # disabled flag -> skip
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    assert await commit_run_changes(repo, task="t", run_id="run_1", files=["x.txt"], deleted=[],
                                    enabled=False) is None
    # a file already identical to HEAD -> nothing to commit
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add x")
    assert await commit_run_changes(repo, task="t", run_id="run_1", files=["x.txt"], deleted=[]) is None
