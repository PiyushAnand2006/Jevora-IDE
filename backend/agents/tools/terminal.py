"""Terminal, test-runner and git tools. Commands run in the sandbox and yield signed evidence."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema
from backend.agents.workspace_state import WorkspaceSnapshot
from backend.core.errors import SandboxUnavailable
from backend.core.proc import run_process, scrubbed_env
from backend.decision.schemas import EvidenceFactory, ExecutionLogPayload, TestResultPayload
from backend.sandbox.base import ExecResult
from backend.sandbox.limits import classify_command, needs_approval


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


class _OutputStreamer:
    """Forwards sandbox output as throttled ``terminal.output`` events from the reader threads."""

    def __init__(self, ctx: ToolContext, command: str) -> None:
        self.ctx = ctx
        self.command = command
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._last = 0.0

    def __call__(self, stream: str, text: str) -> None:
        with self._lock:
            self._buffer.append(text)
            now = time.monotonic()
            if now - self._last < 0.1 and sum(len(b) for b in self._buffer) < 8192:
                return
            data, self._buffer, self._last = "".join(self._buffer), [], now
        self.ctx.emit_threadsafe("terminal.output", {"stream": stream, "data": data, "command": self.command})

    def flush(self) -> None:
        with self._lock:
            data, self._buffer = "".join(self._buffer), []
        if data:
            self.ctx.emit_threadsafe("terminal.output", {"stream": "stdout", "data": data, "command": self.command})


async def execute_in_sandbox(ctx: ToolContext, command: str, cwd: str = ".", timeout: float | None = None,
                             check_risk: bool = True) -> tuple[ExecResult | None, ToolResult | None]:
    """Shared path for every command: risk policy → sandbox → streaming → change detection."""
    if check_risk:
        assessment = classify_command(command)
        if assessment.blocked:
            return None, ToolResult(ok=False, content=f"blocked: command refused ({', '.join(assessment.reasons)})",
                                    data={"risk": "blocked", "reasons": assessment.reasons})
        if needs_approval(ctx.settings.approval_policy, assessment):
            approved = await ctx.request_approval("run_command", {"command": command, "cwd": cwd},
                                                  assessment.reasons)
            if not approved:
                return None, ToolResult(ok=False, content="denied: the user did not approve this command",
                                        data={"risk": assessment.level, "reasons": assessment.reasons})
    try:
        sandbox = await ctx.sandbox()
    except SandboxUnavailable as exc:
        return None, ToolResult(ok=False, content=f"sandbox unavailable: {exc.message}",
                                data={"sandbox_unavailable": True})
    before = WorkspaceSnapshot.capture(ctx.workspace, with_content=False)
    streamer = _OutputStreamer(ctx, command)
    ctx.emit("tool.call", {"tool": "terminal", "command": command, "cwd": cwd, "sandbox": sandbox.kind})
    result = await sandbox.exec(command, cwd=cwd, timeout=timeout or ctx.settings.command_timeout_s,
                                on_output=streamer)
    streamer.flush()
    changes = before.changes()
    for change, paths in (("created", changes["added"]), ("modified", changes["modified"]),
                          ("deleted", changes["deleted"])):
        for rel in paths[:200]:
            ctx.file_changed(rel, change)
    return result, None


class RunCommand(Tool):
    name = "run_command"
    description = ("Run a shell command in the project's sandbox (workspace mounted at the current directory). "
                   "Network is disabled by default. Returns exit code, stdout and stderr.")
    permission = "terminal.run"
    parameters = schema({"command": {"type": "string"}, "cwd": {"type": "string", "default": "."},
                         "timeout": {"type": "integer"}}, ["command"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        command = str(args["command"]).strip()
        if not command:
            return ToolResult(ok=False, content="error: empty command")
        result, failure = await execute_in_sandbox(ctx, command, str(args.get("cwd") or "."),
                                                   float(args["timeout"]) if args.get("timeout") else None)
        if failure is not None:
            return failure
        assert result is not None
        evidence = EvidenceFactory.make("execution_log", "sandbox", ExecutionLogPayload(
            command=command, exit_code=result.exit_code, timed_out=result.timed_out,
            stdout_tail=_tail(result.stdout), stderr_tail=_tail(result.stderr), duration_s=result.duration_s),
            ctx.tool_call_id)
        return ToolResult(ok=result.ok, content=result.summary(), evidence=[evidence],
                          data={"exit_code": result.exit_code, "timed_out": result.timed_out})


# --------------------------------------------------------------------------- tests
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
_JEST_TESTS = re.compile(r"Tests:\s+(.*?)(\d+) total")


def detect_test_command(workspace: Path) -> tuple[str, str] | None:
    def has(*names: str) -> bool:
        return any((workspace / n).exists() for n in names)

    py_tests = (has("pytest.ini", "conftest.py", "tests", "test") or any(workspace.glob("test_*.py"))
                or any(workspace.glob("*_test.py")) or any(workspace.glob("*/test_*.py")))
    if py_tests or (has("pyproject.toml", "setup.py") and any(workspace.rglob("test_*.py"))):
        return "pytest", "python -m pytest -q -rA --tb=short -p no:cacheprovider"
    package = workspace / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        if "test" in scripts:
            return "npm", "npm test --silent"
    if has("go.mod"):
        return "go", "go test ./..."
    if has("Cargo.toml"):
        return "cargo", "cargo test"
    return None


def parse_test_output(framework: str, command: str, result: ExecResult) -> TestResultPayload:
    text = result.stdout + "\n" + result.stderr
    payload = TestResultPayload(framework=framework, command=command, exit_code=result.exit_code,
                                timed_out=result.timed_out, duration_s=result.duration_s,
                                output_tail=_tail(text.strip(), 6000))
    if framework == "pytest":
        summary = ""
        for line in reversed(text.splitlines()):
            if _PYTEST_COUNTS.search(line) or "no tests ran" in line:
                summary = line
                break
        for count, label in _PYTEST_COUNTS.findall(summary):
            n = int(count)
            if label == "passed":
                payload.passed = n
            elif label == "failed":
                payload.failed = n
            elif label.startswith("error"):
                payload.errors = n
            elif label == "skipped":
                payload.skipped = n
        payload.failures = [line.strip() for line in text.splitlines()
                            if line.startswith(("FAILED ", "ERROR "))][:50]
    elif framework == "npm":
        match = _JEST_TESTS.search(text)
        if match:
            for count, label in re.findall(r"(\d+) (passed|failed|skipped)", match.group(1)):
                setattr(payload, label if label != "skipped" else "skipped", int(count))
        else:
            passing = re.search(r"(\d+) passing", text)
            failing = re.search(r"(\d+) failing", text)
            payload.passed = int(passing.group(1)) if passing else 0
            payload.failed = int(failing.group(1)) if failing else 0
    elif framework == "go":
        payload.passed = len(re.findall(r"^ok\s", text, re.M)) + len(re.findall(r"--- PASS", text))
        payload.failed = len(re.findall(r"--- FAIL", text)) or len(re.findall(r"^FAIL\s", text, re.M))
    if payload.passed == payload.failed == payload.errors == 0 and result.exit_code not in (0, 5, None):
        payload.errors = 1  # the runner itself failed (import error, syntax error, missing tool…)
    payload.collected = payload.passed + payload.failed + payload.errors + payload.skipped
    return payload


class RunTests(Tool):
    name = "run_tests"
    description = ("Run the project's automated tests in the sandbox (auto-detects pytest, npm test, go test, "
                   "cargo test) and return a structured pass/fail summary. Optionally pass a custom command "
                   "or a path to a test file/directory.")
    permission = "tests.run"
    parameters = schema({"command": {"type": "string"}, "path": {"type": "string"}})

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "").strip()
        framework = "custom"
        if command:
            framework = "pytest" if "pytest" in command else "npm" if command.startswith("npm") else "custom"
        else:
            detected = detect_test_command(ctx.workspace)
            if detected is None:
                return ToolResult(ok=False, content="error: no test framework detected (add tests, e.g. "
                                                    "test_*.py for pytest, or pass a command)")
            framework, command = detected
            if args.get("path"):
                target = ctx.guard.relative(ctx.guard.resolve(str(args["path"])))
                command = f"{command} {target}"
        result, failure = await execute_in_sandbox(ctx, command, ".", check_risk=bool(args.get("command")))
        if failure is not None:
            return failure
        assert result is not None
        payload = parse_test_output(framework, command, result)
        evidence = EvidenceFactory.make("test_result", "sandbox", payload, ctx.tool_call_id)
        passed = (result.exit_code == 0 and not result.timed_out and payload.failed == 0 and payload.errors == 0)
        ctx.emit("test.result", {"framework": framework, "command": command, "passed": payload.passed,
                                 "failed": payload.failed, "errors": payload.errors, "skipped": payload.skipped,
                                 "exit_code": result.exit_code, "ok": passed, "failures": payload.failures[:10]})
        header = (f"tests {'PASSED' if passed else 'FAILED'}: {payload.passed} passed, {payload.failed} failed, "
                  f"{payload.errors} errors, {payload.skipped} skipped (exit {result.exit_code})")
        return ToolResult(ok=passed, content=header + "\n" + _tail(result.stdout + result.stderr, 8000),
                          evidence=[evidence], data=payload.model_dump())


# --------------------------------------------------------------------------- git
NULL_HOOKS = "NUL" if sys.platform == "win32" else "/dev/null"


async def git(ctx: ToolContext, *args: str, timeout: float = 60) -> tuple[int | None, str]:
    return await git_in(ctx.guard.root, *args, timeout=timeout)


async def git_in(root: Path, *args: str, timeout: float = 60) -> tuple[int | None, str]:
    """Run git in the workspace with repository hooks disabled (hooks would execute code on the host)."""
    argv = ["git", "-C", str(root), "-c", f"core.hooksPath={NULL_HOOKS}", "-c", "core.fsmonitor=false",
            "-c", "safe.directory=*", *args]
    env = scrubbed_env({"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Jevora Agent",
                        "GIT_AUTHOR_EMAIL": "agent@jevora.local", "GIT_COMMITTER_NAME": "Jevora Agent",
                        "GIT_COMMITTER_EMAIL": "agent@jevora.local"})
    result = await run_process(argv, env=env, timeout=timeout)
    return result.exit_code, (result.stdout + result.stderr).strip()


class GitStatus(Tool):
    name = "git_status"
    description = "Show git status (short format) and the current branch of the workspace repository."
    permission = "git.read"
    parameters = schema({})

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        code, out = await git(ctx, "status", "--short", "--branch")
        return ToolResult(ok=code == 0, content=out or "clean")


class GitDiff(Tool):
    name = "git_diff"
    description = "Show the git diff of the working tree (optionally for one path, or staged changes)."
    permission = "git.read"
    parameters = schema({"path": {"type": "string"}, "staged": {"type": "boolean", "default": False}})

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        argv = ["diff", "--no-color"]
        if args.get("staged"):
            argv.append("--staged")
        if args.get("path"):
            argv += ["--", ctx.guard.relative(ctx.guard.resolve(str(args["path"])))]
        code, out = await git(ctx, *argv)
        return ToolResult(ok=code == 0, content=_tail(out, 20000) or "no changes")


class GitLog(Tool):
    name = "git_log"
    description = "Show recent commits (one line each)."
    permission = "git.read"
    parameters = schema({"limit": {"type": "integer", "default": 10}})

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        code, out = await git(ctx, "log", "--oneline", f"-{max(1, min(int(args.get('limit') or 10), 100))}")
        return ToolResult(ok=code == 0, content=out or "no commits")


class GitCommit(Tool):
    name = "git_commit"
    description = "Stage all changes and create a commit (initialises a repository if needed)."
    permission = "git.write"
    parameters = schema({"message": {"type": "string"}}, ["message"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if not (ctx.guard.root / ".git").exists():
            code, out = await git(ctx, "init")
            if code != 0:
                return ToolResult(ok=False, content=f"git init failed: {out}")
        await git(ctx, "add", "-A")
        code, out = await git(ctx, "commit", "-m", str(args["message"])[:2000])
        return ToolResult(ok=code == 0, content=out)


class GitBranch(Tool):
    name = "git_branch"
    description = "Create a branch (and optionally check it out)."
    permission = "git.write"
    parameters = schema({"name": {"type": "string"}, "checkout": {"type": "boolean", "default": True}}, ["name"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = str(args["name"])
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name) or name.startswith("-"):
            return ToolResult(ok=False, content="error: invalid branch name")
        argv = ["checkout", "-b", name] if args.get("checkout", True) else ["branch", name]
        code, out = await git(ctx, *argv)
        return ToolResult(ok=code == 0, content=out or f"created branch {name}")


TERMINAL_TOOLS: list[Tool] = [RunCommand(), RunTests(), GitStatus(), GitDiff(), GitLog(), GitCommit(), GitBranch()]
