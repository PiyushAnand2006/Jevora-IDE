"""Filesystem-backed Obsidian notes for portable Jevora run history."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

from backend.orchestrator.records import CallRecord, RunRecord
from backend.vault.templates import safe_filename


class VaultError(RuntimeError):
    """Raised when a vault cannot be read or updated safely."""


_SECTION_RE = re.compile(
    r"(?P<start><!-- jevora:begin (?P<name>[a-z0-9_-]+) -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- jevora:end (?P=name) -->)",
    re.DOTALL,
)


def _section(name: str, body: str) -> str:
    return f"<!-- jevora:begin {name} -->\n{body.rstrip()}\n<!-- jevora:end {name} -->"


def _replace_section(text: str, name: str, body: str) -> str:
    replacement = _section(name, body)
    pattern = re.compile(
        rf"<!-- jevora:begin {re.escape(name)} -->.*?<!-- jevora:end {re.escape(name)} -->",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(lambda _match: replacement, text, count=1)
    return text.rstrip() + "\n\n" + replacement + "\n"


def _section_body(text: str, name: str) -> str | None:
    for match in _SECTION_RE.finditer(text):
        if match.group("name") == name:
            return match.group("body").strip()
    return None


def _replace_frontmatter(text: str, key: str, value: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    frontmatter = text[4:end]
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    updated, count = pattern.subn(f"{key}: {value}", frontmatter, count=1)
    if not count:
        updated += f"\n{key}: {value}"
    return "---\n" + updated + text[end:]


def _date(value: str | None) -> str:
    return (value or "unknown")[:10]


def _model_label(call: CallRecord) -> str:
    return call.model_used or "no model"


class Vault:
    """Read/write a Jevora vault rooted at ``<path>/Jevora``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.root = self.path / "Jevora"

    def check_writable(self) -> tuple[bool, str | None]:
        """Return whether the vault can be created and written."""

        try:
            if self.path.exists() and not self.path.is_dir():
                return False, f"vault path is not a directory: {self.path}"
            if self.path.exists() and not (self.path.stat().st_mode & 0o222):
                return False, f"vault path is not writable: {self.path}"
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root.stat().st_mode & 0o222):
                return False, f"vault path is not writable: {self.root}"
            probe = self.root / ".jevora-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True, None
        except (OSError, ValueError) as exc:
            return False, f"vault is not writable: {exc}"

    def _ensure_writable(self) -> None:
        ok, error = self.check_writable()
        if not ok:
            raise VaultError(error or "vault is not writable")

    def _run_note_path(self, record: RunRecord) -> Path:
        title = safe_filename(record.task, 70)
        return self.root / "Runs" / f"{title} ({record.run_id}).md"

    def find_run_note(self, run_id: str) -> Path | None:
        runs = self.root / "Runs"
        if not runs.exists():
            return None
        for path in runs.rglob("*.md"):
            if f"({run_id})" in path.name:
                return path
        return None

    def _initial_run_note(self, record: RunRecord, note_path: Path) -> str:
        title = record.task.strip().splitlines()[0][:160] if record.task.strip() else "Jevora run"
        links = "\n".join(
            f"- [[Jevora/Calls/{record.run_id}/{call.call_id}|{call.agent_name or call.role} · {_model_label(call)}]]"
            for call in record.calls
        ) or "- No agent calls recorded."
        verdicts = "\n".join(
            f"- `{decision.choice}` ({decision.engine}, {decision.confidence:.2f})"
            for decision in record.decisions
            if decision.acted
        ) or "- No acted decision recorded."
        diff = f"```diff\n{record.diff.rstrip()}\n```" if record.diff else "No diff recorded."
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        record_section = _section("record", f"```json\n{payload}\n```")
        return (
            "---\n"
            f"run_id: {record.run_id}\n"
            f"created_at: {record.created_at or ''}\n"
            f"status: {record.status}\n"
            f"tags:\n  - jevora/run\n  - jevora/{safe_filename(record.mode, 40)}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"**Mode:** `{record.mode}`  \n**Status:** `{record.status}`  \n"
            f"**Run:** `{record.run_id}`\n\n"
            "_Your notes below are preserved when Jevora updates this note._\n\n"
            f"{record_section}\n\n"
            f"{_section('calls', links)}\n\n"
            f"{_section('decisions', verdicts)}\n\n"
            f"{_section('diff', diff)}\n"
        )

    def _update_run_note(self, existing: str, record: RunRecord) -> str:
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        links = "\n".join(
            f"- [[Jevora/Calls/{record.run_id}/{call.call_id}|{call.agent_name or call.role} · {_model_label(call)}]]"
            for call in record.calls
        ) or "- No agent calls recorded."
        verdicts = "\n".join(
            f"- `{decision.choice}` ({decision.engine}, {decision.confidence:.2f})"
            for decision in record.decisions
            if decision.acted
        ) or "- No acted decision recorded."
        diff = f"```diff\n{record.diff.rstrip()}\n```" if record.diff else "No diff recorded."
        updated = _replace_frontmatter(existing, "status", record.status)
        updated = _replace_section(updated, "record", f"```json\n{payload}\n```")
        updated = _replace_section(updated, "calls", links)
        updated = _replace_section(updated, "decisions", verdicts)
        return _replace_section(updated, "diff", diff)

    def _write_call_notes(self, record: RunRecord) -> None:
        calls_root = self.root / "Calls" / record.run_id
        calls_root.mkdir(parents=True, exist_ok=True)
        by_id = {call.call_id: call for call in record.calls}
        for call in record.calls:
            parent = by_id.get(call.parent_call_id or "")
            parent_link = (
                f"Parent: [[Jevora/Calls/{record.run_id}/{parent.call_id}|"
                f"{parent.agent_name or parent.role} · {_model_label(parent)}]]\n\n"
                if parent is not None
                else ""
            )
            children = [child for child in record.calls if child.parent_call_id == call.call_id]
            children_text = "\n".join(
                f"- [[Jevora/Calls/{record.run_id}/{child.call_id}|"
                f"{child.agent_name or child.role} · {_model_label(child)}]]"
                for child in children
            ) or "- None"
            payload = json.dumps(call.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
            call_section = _section("call", f"```json\n{payload}\n```")
            content = (
                f"# {call.agent_name or call.role} · {_model_label(call)}\n\n"
                f"{parent_link}"
                f"**Status:** `{call.status}`  **Tokens:** `{call.tokens}`\n\n"
                f"{call_section}\n\n"
                f"{_section('children', children_text)}\n"
            )
            (calls_root / f"{safe_filename(call.call_id, 80)}.md").write_text(content, encoding="utf-8")

    def _update_index(self, record: RunRecord, note_path: Path) -> None:
        index = self.root / "Jevora Index.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        text = index.read_text(encoding="utf-8") if index.exists() else "# Jevora Index\n\n"
        marker = f"<!-- key:{record.run_id} -->"
        line = (
            f"{marker}\n- [[Jevora/{note_path.relative_to(self.root).as_posix()}|"
            f"{safe_filename(record.task, 100)}]] · `{record.status}`\n"
        )
        pattern = re.compile(rf"<!-- key:{re.escape(record.run_id)} -->\n(?:- .*\n)?")
        text = pattern.sub(line, text)
        if marker not in text:
            text = text.rstrip() + "\n\n" + line
        index.write_text(text, encoding="utf-8")

    def _update_daily(self, record: RunRecord, note_path: Path) -> None:
        daily = self.root / "Daily" / f"{_date(record.created_at)}.md"
        daily.parent.mkdir(parents=True, exist_ok=True)
        text = daily.read_text(encoding="utf-8") if daily.exists() else f"# {_date(record.created_at)}\n\n"
        marker = f"<!-- key:{record.run_id} -->"
        line = (
            f"{marker}\n- [[Jevora/{note_path.relative_to(self.root).as_posix()}|"
            f"{safe_filename(record.task, 100)}]] · `{record.status}`\n"
        )
        pattern = re.compile(rf"<!-- key:{re.escape(record.run_id)} -->\n(?:- .*\n)?")
        text = pattern.sub(line, text)
        if marker not in text:
            text = text.rstrip() + "\n\n" + line
        daily.write_text(text, encoding="utf-8")

    def _update_agent_notes(self, record: RunRecord, note_path: Path) -> None:
        for agent in {call.agent_name or call.role for call in record.calls}:
            path = self.root / "Agents" / f"{safe_filename(agent, 80)}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8") if path.exists() else f"# {agent}\n\n"
            marker = f"<!-- key:{record.run_id} -->"
            line = f"{marker}\n- [[Jevora/{note_path.relative_to(self.root).as_posix()}|{record.run_id}]]\n"
            pattern = re.compile(rf"<!-- key:{re.escape(record.run_id)} -->\n(?:- .*\n)?")
            text = pattern.sub(line, text)
            if marker not in text:
                text = text.rstrip() + "\n\n" + line
            path.write_text(text, encoding="utf-8")

    def write_run(self, record: RunRecord) -> Path:
        """Write or update all generated notes for a run."""

        self._ensure_writable()
        note_path = self.find_run_note(record.run_id) or self._run_note_path(record)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        if note_path.exists():
            content = self._update_run_note(note_path.read_text(encoding="utf-8"), record)
        else:
            content = self._initial_run_note(record, note_path)
        note_path.write_text(content, encoding="utf-8")
        self._write_call_notes(record)
        self._update_index(record, note_path)
        self._update_daily(record, note_path)
        self._update_agent_notes(record, note_path)
        return note_path

    def _record_from_note(self, path: Path) -> RunRecord | None:
        try:
            text = path.read_text(encoding="utf-8")
            body = _section_body(text, "record")
            if not body:
                return None
            if body.startswith("```json"):
                body = body[len("```json") :].strip()
                if body.endswith("```"):
                    body = body[:-3].strip()
            return RunRecord.model_validate(json.loads(body))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def read_run(self, run_id: str) -> RunRecord | None:
        note = self.find_run_note(run_id)
        return self._record_from_note(note) if note is not None else None

    def iter_records(self) -> Iterator[RunRecord]:
        runs = self.root / "Runs"
        if not runs.exists():
            return
        for path in sorted(runs.rglob("*.md")):
            record = self._record_from_note(path)
            if record is not None:
                yield record

    def delete_run(self, run_id: str) -> int:
        """Delete generated run/call notes and remove managed links.

        The return value counts deleted note files, which keeps the operation
        useful to callers while leaving user-authored daily/index text intact.
        """

        removed = 0
        note = self.find_run_note(run_id)
        if note is not None:
            note.unlink(missing_ok=True)
            removed += 1
        calls = self.root / "Calls" / run_id
        if calls.exists():
            for path in calls.glob("*.md"):
                path.unlink(missing_ok=True)
                removed += 1
            with contextlib.suppress(OSError):
                calls.rmdir()
        for path in self.root.rglob("*.md") if self.root.exists() else ():
            if path.name in {"Jevora Index.md"} or path.parent.name == "Daily" or path.parent.name == "Agents":
                text = path.read_text(encoding="utf-8")
                pattern = re.compile(rf"<!-- key:{re.escape(run_id)} -->\n(?:- .*\n)?")
                updated = pattern.sub("", text)
                if updated != text:
                    path.write_text(updated, encoding="utf-8")
        return removed
