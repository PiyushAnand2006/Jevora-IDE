"""Apply unified diffs produced by LLMs (tolerant to small line-offset drift)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+'

    @property
    def before(self) -> list[str]:
        return [line[1:] for line in self.lines if line[:1] in (" ", "-")]

    @property
    def after(self) -> list[str]:
        return [line[1:] for line in self.lines if line[:1] in (" ", "+")]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_delete(self) -> bool:
        return self.new_path is None


def _clean_path(raw: str) -> str | None:
    raw = raw.strip().split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.replace("\r\n", "\n").split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_clean_path(line[4:]), _clean_path(lines[i + 1][4:]))
            patches.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK_RE.match(line)
        if match and current is not None:
            hunk = Hunk(old_start=int(match.group(1)))
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line == "" and i < len(lines) - 1:
            hunk.lines.append(" ")  # blank context line whose leading space was stripped
        elif line.startswith("\\ No newline"):
            pass
        i += 1
    if not patches:
        raise PatchError("no file headers (---/+++) found in patch")
    return patches


def _find(haystack: list[str], needle: list[str], hint: int) -> int:
    if not needle:
        return max(0, min(hint, len(haystack)))
    candidates = [hint + d for pair in ((0,), *((k, -k) for k in range(1, len(haystack) + 1))) for d in pair]
    for start in candidates:
        if 0 <= start <= len(haystack) - len(needle) and haystack[start:start + len(needle)] == needle:
            return start
    stripped = [h.rstrip() for h in haystack]
    target = [n.rstrip() for n in needle]
    for start in range(0, len(haystack) - len(needle) + 1):
        if stripped[start:start + len(needle)] == target:
            return start
    return -1


def apply_hunks(original: str, patch: FilePatch) -> str:
    trailing_newline = original.endswith("\n") or original == ""
    lines = original.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    offset = 0
    for number, hunk in enumerate(patch.hunks, 1):
        before, after = hunk.before, hunk.after
        start = _find(lines, before, max(0, hunk.old_start - 1 + offset))
        if start < 0:
            raise PatchError(f"hunk {number} for {patch.path} does not match the current file content")
        lines[start:start + len(before)] = after
        offset += len(after) - len(before)
    text = "\n".join(lines)
    return text + "\n" if (trailing_newline or patch.is_new) and text else text
