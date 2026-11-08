"""Auto-commit verified work on the workspace's current git branch.

Mirrors what Codex/ZCode leave behind: after the decision layer confirms a
build-mode run, exactly the files that run changed are staged and committed
(current branch, author "Jevora Agent", repository hooks disabled). Never
initialises a repository and never stages files outside the run's change set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.agents.tools.terminal import git_in
from backend.core.logging import get_logger

log = get_logger(__name__)


def _enclosing_repo(workspace: Path) -> Path | None:
    """Walk up (bounded) from the workspace to the nearest enclosing .git."""
    current = workspace.resolve()
    for _ in range(6):
        if (current / ".git").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


async def commit_run_changes(workspace: Path, *, task: str, run_id: str, files: list[str],
                             deleted: list[str], enabled: bool = True) -> dict[str, Any] | None:
    """Stage the run's changed files and commit them. ``None`` when skipped.

    Skips when disabled, no enclosing git repository, nothing to commit, or git
    fails (logged, never raised — commit is best-effort).
    """
    repo = _enclosing_repo(workspace) if enabled else None
    if repo is None or not (files or deleted):
        return None
    depth = len(workspace.resolve().relative_to(repo).parts) if workspace.resolve() != repo else 0
    prefixes = ("../" * depth) if depth else ""
    paths = [prefixes + p for p in [*files, *deleted]]
    code, out = await git_in(repo, "add", "-A", "--", *paths)
    if code != 0:
        log.warning("auto-commit staging failed: %s", out[-300:])
        return None
    code, status = await git_in(repo, "status", "--short")
    staged = [line for line in status.splitlines()
              if line.strip() and not line.startswith("??")] if code == 0 else []
    if not staged:
        return None  # nothing differs from HEAD
    subject = (task or "Jevora run").strip().splitlines()[0][:72]
    message = f"{subject} (jevora run {run_id[-8:]})"
    code, out = await git_in(repo, "commit", "-m", message)
    if code != 0:
        log.warning("auto-commit failed: %s", out[-300:])
        return None
    _, sha_out = await git_in(repo, "rev-parse", "--short", "HEAD")
    sha = sha_out.splitlines()[0].strip() if sha_out else ""
    return {"sha": sha, "message": message, "files": len(staged)}
