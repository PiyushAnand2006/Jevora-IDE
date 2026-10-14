"""Workspace snapshots: per-run unified diffs and change detection after terminal commands."""

from __future__ import annotations

import difflib
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.paths import IGNORED_DIRS, is_binary

MAX_FILES = 5000
MAX_TEXT_BYTES = 256 * 1024
MAX_TOTAL_TEXT = 64 * 1024 * 1024


def iter_files(root: Path, max_files: int = MAX_FILES) -> Iterator[tuple[str, os.stat_result]]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".jevora"))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            if not full.is_file():
                continue
            yield full.relative_to(root).as_posix(), st
            count += 1
            if count >= max_files:
                return


@dataclass
class FileEntry:
    size: int
    mtime_ns: int
    sha1: str | None = None
    text: str | None = None
    binary: bool = False


@dataclass
class WorkspaceSnapshot:
    root: Path
    files: dict[str, FileEntry] = field(default_factory=dict)

    @classmethod
    def capture(cls, root: Path, with_content: bool = True) -> WorkspaceSnapshot:
        snap = cls(root=root)
        total = 0
        for rel, st in iter_files(root):
            entry = FileEntry(size=st.st_size, mtime_ns=st.st_mtime_ns)
            if with_content and st.st_size <= MAX_TEXT_BYTES and total < MAX_TOTAL_TEXT:
                try:
                    data = (root / rel).read_bytes()
                except OSError:
                    data = b""
                entry.sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
                entry.binary = is_binary(data[:8192])
                if not entry.binary:
                    entry.text = data.decode("utf-8", "replace")
                    total += len(data)
            snap.files[rel] = entry
        return snap

    def changes(self) -> dict[str, list[str]]:
        """Cheap (size/mtime) comparison with the current workspace."""
        current = {rel: st for rel, st in iter_files(self.root)}
        added = sorted(set(current) - set(self.files))
        deleted = sorted(set(self.files) - set(current))
        modified = sorted(rel for rel in set(current) & set(self.files)
                          if current[rel].st_size != self.files[rel].size
                          or current[rel].st_mtime_ns != self.files[rel].mtime_ns)
        return {"added": added, "modified": modified, "deleted": deleted}

    def to_dict(self) -> dict[str, Any]:
        return {"root": str(self.root), "files": {k: [v.size, v.mtime_ns, v.sha1, v.binary] for k, v in
                                                  self.files.items()}}


def _read_text(path: Path) -> tuple[str | None, bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, False
    if is_binary(data[:8192]):
        return None, True
    return data.decode("utf-8", "replace"), False


def unified_diff(snapshot: WorkspaceSnapshot, max_chars: int = 400_000) -> tuple[str, dict[str, Any]]:
    """Diff the snapshot against the current workspace. Returns (diff text, stats)."""
    root = snapshot.root
    changes = snapshot.changes()
    chunks: list[str] = []
    additions = deletions = 0
    files: list[str] = []
    for rel in sorted(changes["added"] + changes["modified"]):
        old = snapshot.files.get(rel)
        new_text, new_binary = _read_text(root / rel)
        if old is not None and old.sha1 is not None and not new_binary and new_text is not None:
            digest = hashlib.sha1(new_text.encode("utf-8"), usedforsecurity=False).hexdigest()
            if digest == old.sha1 and old.text == new_text:
                continue  # touched, not changed
        files.append(rel)
        if new_binary or (old is not None and old.binary):
            chunks.append(f"Binary file {rel} changed\n")
            continue
        old_lines = (old.text or "").splitlines(keepends=True) if old is not None and old.text is not None else []
        if old is not None and old.text is None and not old.binary:
            chunks.append(f"File {rel} changed (too large to diff)\n")
            continue
        new_lines = (new_text or "").splitlines(keepends=True)
        diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="/dev/null" if old is None else f"a/{rel}",
                                         tofile=f"b/{rel}", n=3))
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        chunks.append("".join(d if d.endswith("\n") else d + "\n\\ No newline at end of file\n" for d in diff))
    for rel in changes["deleted"]:
        old = snapshot.files[rel]
        files.append(rel)
        if old.text is None:
            chunks.append(f"Deleted file {rel}\n")
            continue
        old_lines = old.text.splitlines(keepends=True)
        deletions += len(old_lines)
        removed = difflib.unified_diff(old_lines, [], fromfile=f"a/{rel}", tofile="/dev/null", n=3)
        chunks.append("".join(d if d.endswith("\n") else d + "\n" for d in removed))
    text = "".join(chunks)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n…[diff truncated]\n"
    stats = {"files_changed": len(files), "additions": additions, "deletions": deletions, "files": files,
             "deleted_files": changes["deleted"], "truncated": truncated}
    return text, stats


def save_snapshot(snapshot: WorkspaceSnapshot, path: Path) -> None:
    """Persist a snapshot (gzip JSON) so a resumed run still diffs against its true starting point."""
    import gzip
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"root": str(snapshot.root), "files": {rel: [e.size, e.mtime_ns, e.sha1, e.text, e.binary]
                                                  for rel, e in snapshot.files.items()}}
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def load_snapshot(path: Path) -> WorkspaceSnapshot | None:
    import gzip
    import json

    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    snap = WorkspaceSnapshot(root=Path(data["root"]))
    for rel, (size, mtime_ns, sha1, text, binary) in data["files"].items():
        snap.files[rel] = FileEntry(size=size, mtime_ns=mtime_ns, sha1=sha1, text=text, binary=binary)
    return snap
