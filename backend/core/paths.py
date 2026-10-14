"""Workspace confinement: every agent file path is resolved and checked against the workspace root."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from backend.core.errors import PermissionDenied

IGNORED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
                          ".ruff_cache", ".jevora", ".idea", ".vscode", "dist", "build", ".next", ".tox"})
PROTECTED_WRITE_DIRS = (".git", ".jevora")
_WINDOWS_DEVICES = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


class WorkspaceGuard:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(root))

    def _inside(self, candidate: Path) -> bool:
        root = os.path.normcase(str(self.root))
        path = os.path.normcase(str(candidate))
        return path == root or path.startswith(root.rstrip("\\/") + os.sep)

    def resolve(self, path: str | os.PathLike[str], *, for_write: bool = False, allow_internal: bool = False) -> Path:
        """Resolve ``path`` inside the workspace, following symlinks; raise PermissionDenied on escape."""
        raw = os.fspath(path)
        if "\x00" in raw:
            raise PermissionDenied("path contains a NUL byte")
        raw = raw.strip() or "."
        if raw.startswith(("\\\\", "//")) or raw.startswith("\\\\?\\"):
            raise PermissionDenied("UNC/device paths are not allowed")
        candidate = Path(raw)
        if sys.platform == "win32":
            for part in candidate.parts[1 if candidate.is_absolute() else 0:]:
                if ":" in part:
                    raise PermissionDenied("alternate data streams are not allowed")
                if _WINDOWS_DEVICES.match(part):
                    raise PermissionDenied(f"reserved device name '{part}' is not allowed")
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = Path(os.path.realpath(candidate))  # follows every symlink in the chain
        if not self._inside(resolved):
            raise PermissionDenied(f"path '{raw}' resolves outside the workspace")
        if for_write and not allow_internal:
            rel = self.relative(resolved)
            first = rel.split("/", 1)[0]
            if first in PROTECTED_WRITE_DIRS:
                raise PermissionDenied(f"writing inside '{first}/' is not allowed")
        return resolved

    def relative(self, path: Path) -> str:
        try:
            rel = Path(os.path.realpath(path)).relative_to(self.root)
        except ValueError:
            rel = Path(os.path.relpath(os.path.realpath(path), self.root))
        text = rel.as_posix()
        return "." if text in ("", ".") else text


def is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as exc:
        # A multi-byte sequence cut at the sample boundary is still text.
        return exc.start < len(sample) - 4


LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".json": "json", ".md": "markdown",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".bat": "batch", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".sql": "sql", ".xml": "xml", ".svg": "xml", ".txt": "plaintext", ".csv": "csv", ".dockerfile": "dockerfile",
    ".vue": "vue", ".svelte": "svelte", ".lua": "lua", ".r": "r", ".dart": "dart",
}
SPECIAL_NAMES = {"dockerfile": "dockerfile", "makefile": "makefile", "justfile": "makefile",
                 ".gitignore": "ignore", ".env": "dotenv"}


def detect_language(path: str) -> str:
    name = Path(path).name.lower()
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    return LANGUAGES.get(Path(path).suffix.lower(), "plaintext")
