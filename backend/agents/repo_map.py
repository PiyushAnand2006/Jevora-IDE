"""Aider-style repo map: a compact directory + symbol outline for agent context.

Instead of reading every file, the agent gets one capped block describing the
workspace structure and the key symbols (functions/classes) of source files, so
large repositories fit the context window. Cached until the workspace's file
signature (count + newest mtime) changes.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.agents.workspace_state import iter_files
from backend.core.logging import get_logger

log = get_logger(__name__)

MAX_MAP_CHARS = 6_000
MAX_FILES_IN_MAP = 400
MAX_SYMS_PER_FILE = 8
_READ_BYTES = 96 * 1024

_IDENT = r"[A-Za-z_$][\w$]*"
SYMBOL_RES: dict[str, list[re.Pattern[str]]] = {
    ".py": [re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)"), re.compile(r"^class\s+([A-Za-z_]\w*)")],
    ".js": [re.compile(rf"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+({_IDENT})"),
            re.compile(rf"(?:export\s+)?class\s+({_IDENT})"),
            re.compile(rf"^export\s+const\s+({_IDENT})")],
    ".jsx": [re.compile(rf"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+({_IDENT})"),
             re.compile(rf"(?:export\s+)?class\s+({_IDENT})")],
    ".mjs": [re.compile(rf"(?:export\s+)?(?:async\s+)?function\s+({_IDENT})")],
    ".cjs": [re.compile(rf"(?:async\s+)?function\s+({_IDENT})")],
    ".ts": [re.compile(rf"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+({_IDENT})"),
            re.compile(rf"(?:export\s+)?(?:abstract\s+)?class\s+({_IDENT})"),
            re.compile(rf"^export\s+(?:const|interface|type)\s+({_IDENT})")],
    ".tsx": [re.compile(rf"(?:export\s+)?(?:default\s+)?function\s+({_IDENT})"),
             re.compile(rf"(?:export\s+)?class\s+({_IDENT})"),
             re.compile(rf"^export\s+(?:const|interface|type)\s+({_IDENT})")],
    ".go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")],
    ".rs": [re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")],
}
SUPPORTED = frozenset(SYMBOL_RES)

_cache: dict[tuple[str, int, int], str] = {}


def _extract_symbols(path: Path) -> list[str]:
    try:
        data = path.read_bytes()[:_READ_BYTES]
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    symbols: list[str] = []
    for line in text.splitlines():
        for pattern in SYMBOL_RES.get(path.suffix.lower(), []):
            match = pattern.match(line)
            if match:
                symbols.append(match.group(1))
                break
        if len(symbols) >= MAX_SYMS_PER_FILE:
            break
    return symbols


def _signature(root: Path) -> tuple[int, int]:
    count, newest = 0, 0
    for _, st in iter_files(root):
        count += 1
        newest = max(newest, int(st.st_mtime_ns))
    return count, newest


def build_repo_map(workspace: Path, max_chars: int = MAX_MAP_CHARS) -> str:
    """Render the outline; cached per workspace until its file signature changes."""
    try:
        signature = _signature(workspace)
    except OSError:
        return ""
    if not signature[0]:
        return ""
    key = (str(workspace), *signature)
    cached = _cache.get(key)
    if cached is not None:
        return cached[:max_chars]

    by_dir: dict[str, list[tuple[str, list[str]]]] = {}
    for count, (rel, _) in enumerate(iter_files(workspace)):
        if count >= MAX_FILES_IN_MAP:
            break
        path = workspace / rel
        suffix = path.suffix.lower()
        symbols = _extract_symbols(path) if suffix in SUPPORTED else []
        parent = str(Path(rel).parent)
        parent = "" if parent == "." else parent
        by_dir.setdefault(parent, []).append((path.name, symbols))

    lines: list[str] = []
    total = 0
    for directory in sorted(by_dir):
        header = f"{directory}/" if directory else "(root)"
        lines.append(header)
        total += len(header) + 1
        for name, symbols in sorted(by_dir[directory]):
            entry = f"  {name}"
            if symbols:
                entry += " - " + " · ".join(symbols)
            lines.append(entry)
            total += len(entry) + 1
            if total >= max_chars:
                break
        if total >= max_chars:
            lines.append("…[map truncated]")
            break

    text = "\n".join(lines)
    for stale in [k for k in _cache if k[0] == str(workspace)]:
        _cache.pop(stale, None)
    _cache[key] = text
    return text[:max_chars]
