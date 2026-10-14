"""File tools: read, list, search (ripgrep), write, edit, apply_patch, delete."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from backend.agents.patching import PatchError, apply_hunks, parse_patch
from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema
from backend.agents.workspace_state import iter_files
from backend.core.paths import IGNORED_DIRS, is_binary
from backend.core.proc import run_process

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_RETURN_CHARS = 60_000


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jevora-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file from the workspace. Optionally pass start_line/end_line (1-based, inclusive)."
    permission = "fs.read"
    parameters = schema({"path": {"type": "string"}, "start_line": {"type": "integer"},
                         "end_line": {"type": "integer"}}, ["path"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.guard.resolve(str(args["path"]))
        if not path.is_file():
            return ToolResult(ok=False, content=f"error: {args['path']} is not a file")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult(ok=False, content=f"error: file is {size} bytes; too large to read whole "
                                                "(use start_line/end_line or search_files)")
        data = path.read_bytes()
        if is_binary(data[:8192]):
            return ToolResult(content=f"{args['path']} is a binary file ({size} bytes); contents not shown",
                              data={"binary": True, "size": size})
        lines = data.decode("utf-8", "replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or len(lines)))
        body = "\n".join(lines[start - 1:end])
        if len(body) > MAX_RETURN_CHARS:
            body = body[:MAX_RETURN_CHARS] + "\n…[truncated]"
        rel = ctx.guard.relative(path)
        return ToolResult(content=f"{rel} (lines {start}-{end} of {len(lines)}):\n{body}",
                          data={"path": rel, "lines": len(lines)})


class ListDir(Tool):
    name = "list_dir"
    description = "List files and folders under a workspace directory (ignores .git, node_modules, venvs)."
    permission = "fs.read"
    parameters = schema({"path": {"type": "string", "default": "."}, "depth": {"type": "integer", "default": 2}})

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        base = ctx.guard.resolve(str(args.get("path") or "."))
        if not base.is_dir():
            return ToolResult(ok=False, content=f"error: {args.get('path')} is not a directory")
        depth = max(1, min(int(args.get("depth") or 2), 6))
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            rel_depth = len(Path(dirpath).relative_to(base).parts)
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            if rel_depth >= depth:
                dirnames[:] = []
            indent = "  " * rel_depth
            if rel_depth:
                out.append(f"{indent[:-2]}{Path(dirpath).name}/")
            for name in sorted(filenames):
                out.append(f"{indent}{name}")
            if len(out) > 400:
                out.append("…[truncated]")
                break
        rel = ctx.guard.relative(base)
        return ToolResult(content=f"{rel}/\n" + ("\n".join(out) or "(empty)"), data={"entries": len(out)})


class SearchFiles(Tool):
    name = "search_files"
    description = "Search file contents with a regular expression (ripgrep). Returns path:line: text matches."
    permission = "fs.search"
    parameters = schema({"pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
                         "glob": {"type": "string"}, "max_results": {"type": "integer", "default": 100}},
                        ["pattern"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        base = ctx.guard.resolve(str(args.get("path") or "."))
        pattern = str(args["pattern"])
        limit = max(1, min(int(args.get("max_results") or 100), 500))
        matches: list[str] = []
        rg = shutil.which("rg")
        if rg:
            argv = [rg, "--json", "--max-count", "50", "--max-filesize", "2M"]
            for ignored in IGNORED_DIRS:
                argv += ["-g", f"!{ignored}"]
            if args.get("glob"):
                argv += ["-g", str(args["glob"])]
            argv += ["--", pattern, str(base)]
            result = await run_process(argv, cwd=ctx.guard.root, timeout=30)
            for line in result.stdout.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "match":
                    continue
                data = item["data"]
                rel = ctx.guard.relative(Path(data["path"]["text"]))
                text = (data.get("lines", {}).get("text") or "").rstrip("\n")
                matches.append(f"{rel}:{data['line_number']}: {text[:300]}")
                if len(matches) >= limit:
                    break
            if result.exit_code not in (0, 1) and not matches:
                return ToolResult(ok=False, content=f"error: search failed: {result.stderr.strip()[:300]}")
        else:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return ToolResult(ok=False, content=f"error: invalid regex: {exc}")
            for rel, _ in iter_files(base):
                path = base / rel
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if is_binary(data[:8192]):
                    continue
                for number, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if regex.search(text):
                        matches.append(f"{ctx.guard.relative(path)}:{number}: {text[:300]}")
                        if len(matches) >= limit:
                            break
                if len(matches) >= limit:
                    break
        return ToolResult(content="\n".join(matches) or "no matches", data={"count": len(matches)})


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a text file in the workspace with the given full content."
    permission = "fs.write"
    parameters = schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.guard.resolve(str(args["path"]), for_write=True)
        if path.is_dir():
            return ToolResult(ok=False, content=f"error: {args['path']} is a directory")
        existed = path.exists()
        content = str(args["content"])
        _atomic_write(path, content)
        rel = ctx.guard.relative(path)
        ctx.file_changed(rel, "modified" if existed else "created", len(content.encode("utf-8")))
        return ToolResult(content=f"{'updated' if existed else 'created'} {rel} ({len(content)} chars)",
                          files_changed=[rel])


class EditFile(Tool):
    name = "edit_file"
    description = ("Replace an exact snippet in a file. old_text must match exactly once unless "
                   "replace_all is true. Use for small, targeted edits.")
    permission = "fs.write"
    parameters = schema({"path": {"type": "string"}, "old_text": {"type": "string"},
                         "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}},
                        ["path", "old_text", "new_text"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.guard.resolve(str(args["path"]), for_write=True)
        if not path.is_file():
            return ToolResult(ok=False, content=f"error: {args['path']} does not exist (use write_file)")
        text = path.read_text(encoding="utf-8")
        old, new = str(args["old_text"]), str(args["new_text"])
        count = text.count(old) if old else 0
        if count == 0:
            return ToolResult(ok=False, content="error: old_text not found in file; read the file and retry")
        if count > 1 and not args.get("replace_all"):
            return ToolResult(ok=False, content=f"error: old_text matches {count} times; add context or set "
                                                "replace_all")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        _atomic_write(path, updated)
        rel = ctx.guard.relative(path)
        ctx.file_changed(rel, "modified", len(updated.encode("utf-8")))
        return ToolResult(content=f"edited {rel} ({count if args.get('replace_all') else 1} replacement(s))",
                          files_changed=[rel])


class ApplyPatch(Tool):
    name = "apply_patch"
    description = "Apply a unified diff (---/+++ headers, @@ hunks) to one or more workspace files."
    permission = "fs.write"
    parameters = schema({"patch": {"type": "string"}}, ["patch"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        try:
            patches = parse_patch(str(args["patch"]))
        except PatchError as exc:
            return ToolResult(ok=False, content=f"error: {exc}")
        staged: list[tuple[Path, str | None, str]] = []
        for patch in patches:
            target = ctx.guard.resolve(patch.path, for_write=True)
            if patch.is_delete:
                staged.append((target, None, "deleted"))
                continue
            original = "" if patch.is_new else (target.read_text(encoding="utf-8") if target.exists() else "")
            if not patch.is_new and not target.exists():
                return ToolResult(ok=False, content=f"error: {patch.path} does not exist")
            try:
                staged.append((target, apply_hunks(original, patch), "created" if patch.is_new else "modified"))
            except PatchError as exc:
                return ToolResult(ok=False, content=f"error: {exc} (no files were changed)")
        changed: list[str] = []
        for target, content, change in staged:
            rel = ctx.guard.relative(target)
            if content is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, content)
            changed.append(rel)
            ctx.file_changed(rel, change, None if content is None else len(content.encode("utf-8")))
        return ToolResult(content="patched " + ", ".join(changed), files_changed=changed)


class DeleteFile(Tool):
    name = "delete_file"
    description = "Delete a file from the workspace (may require user approval)."
    permission = "fs.write"
    parameters = schema({"path": {"type": "string"}}, ["path"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.guard.resolve(str(args["path"]), for_write=True)
        if not path.is_file():
            return ToolResult(ok=False, content=f"error: {args['path']} is not a file")
        rel = ctx.guard.relative(path)
        if ctx.settings.approval_policy != "auto" and not await ctx.request_approval(
                "delete_file", {"path": rel}, ["file deletion"]):
            return ToolResult(ok=False, content=f"denied: the user did not approve deleting {rel}")
        path.unlink()
        ctx.file_changed(rel, "deleted")
        return ToolResult(content=f"deleted {rel}", files_changed=[rel])


FS_TOOLS: list[Tool] = [ReadFile(), ListDir(), SearchFiles(), WriteFile(), EditFile(), ApplyPatch(), DeleteFile()]
