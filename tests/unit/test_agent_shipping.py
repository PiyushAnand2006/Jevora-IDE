"""Plan parsing, snapshot restore, auto-commit, instructions file, @-mentions."""

import subprocess
from pathlib import Path

import pytest

from backend.agents.repo_map import build_repo_map
from backend.agents.workspace_state import (
    WorkspaceSnapshot,
    load_snapshot,
    read_project_instructions,
    restore_snapshot,
    save_snapshot,
)
from backend.core.paths import WorkspaceGuard
from backend.orchestrator.autocommit import commit_run_changes
from backend.orchestrator.commands import load_workspace_command, workspace_command_list
from backend.orchestrator.nodes.common import build_mention_context
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


# --------------------------------------------------------------------------- instructions file
def test_read_project_instructions_prefers_jevora_md(tmp_path) -> None:
    (tmp_path / "JEVORA.md").write_text("jevora rules\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agent rules\n", encoding="utf-8")
    text, name = read_project_instructions(tmp_path)
    assert name == "JEVORA.md" and "jevora rules" in text


def test_read_project_instructions_fallback_and_missing(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("agent rules\n", encoding="utf-8")
    text, name = read_project_instructions(tmp_path)
    assert name == "AGENTS.md" and "agent rules" in text
    assert read_project_instructions(tmp_path / "nope") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "JEVORA.md").write_text("   \n", encoding="utf-8")
    assert read_project_instructions(empty) is None


# --------------------------------------------------------------------------- @-mentions
def test_build_mention_context_resolves_and_caps(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    guard = WorkspaceGuard(tmp_path)

    context = build_mention_context(guard, ["src/main.py", "blob.bin", "missing.py", "../escape.txt"])
    assert "@src/main.py" in context and "print('hello')" in context
    assert "binary file" in context and "@blob.bin" in context
    assert "missing.py" not in context and "escape" not in context
    assert build_mention_context(guard, []) == ""


# --------------------------------------------------------------------------- repo map
def test_build_repo_map_symbols_and_groups(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "import os\n\n\ndef main():\n    pass\n\n\nclass App:\n    pass\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "export function helper() {}\n\nclass Widget {}\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# not code\n", encoding="utf-8")

    repo_map = build_repo_map(tmp_path)
    assert "src/" in repo_map and "main.py" in repo_map
    assert "def main" in repo_map or "main" in repo_map
    assert "App" in repo_map
    assert "helper" in repo_map and "Widget" in repo_map
    assert "notes.md" in repo_map and "not code" not in repo_map  # listed, no symbols
    assert len(repo_map) <= 6_000


def test_build_repo_map_empty_and_cache(tmp_path) -> None:
    assert build_repo_map(tmp_path) == ""
    (tmp_path / "a.py").write_text("def first():\n    pass\n", encoding="utf-8")
    first = build_repo_map(tmp_path)
    assert "first" in first
    # cached: same content until the signature changes
    assert build_repo_map(tmp_path) == first


# --------------------------------------------------------------------------- custom commands
def test_load_workspace_command(tmp_path) -> None:
    d = tmp_path / ".jevora" / "commands"
    d.mkdir(parents=True)
    (d / "deploy.md").write_text(
        "mode: build\nDeploy the app. Target: {{args}}\nVerify the health endpoint after.\n",
        encoding="utf-8")
    (d / "Not Valid.md").write_text("ignored\n", encoding="utf-8")

    loaded = load_workspace_command(tmp_path, "/deploy production")
    assert loaded is not None
    prompt, mode = loaded
    assert mode == "build"
    assert "Target: production" in prompt and "User arguments" not in prompt
    assert load_workspace_command(tmp_path, "/nonexistent") is None
    assert load_workspace_command(tmp_path, "not a command") is None
    assert load_workspace_command(tmp_path, "/Not Valid") is None


def test_load_workspace_command_default_mode_and_arg_append(tmp_path) -> None:
    d = tmp_path / ".jevora" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("Review the code carefully.\n", encoding="utf-8")
    prompt, mode = load_workspace_command(tmp_path, "/review the auth module")
    assert mode == "build"
    assert prompt == "Review the code carefully.\n\nUser arguments: the auth module"


def test_workspace_command_list(tmp_path) -> None:
    d = tmp_path / ".jevora" / "commands"
    d.mkdir(parents=True)
    (d / "deploy.md").write_text("# Deploy the service\nmode: build\nbody", encoding="utf-8")
    (d / "skip me.md").write_text("x", encoding="utf-8")
    listed = workspace_command_list(tmp_path)
    assert len(listed) == 1
    assert listed[0]["name"] == "/deploy" and "Deploy the service" in listed[0]["description"]
