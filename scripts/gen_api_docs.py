"""Generate docs/openapi.json and docs/API.md from the FastAPI app."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.main import create_app  # noqa: E402

WEBSOCKETS = """
## WebSockets & streaming

| Endpoint | Purpose |
|---|---|
| `WS /api/runs/{id}/stream?last_seq=N&token=JWT` | Typed events for one run. Reconnect with the last `seq` you saw to replay missed events. A fresh connection starts with a `run.status` snapshot. |
| `GET /api/runs/{id}/events?last_seq=N` (SSE, also honours `Last-Event-ID`) | Same events as Server-Sent Events; closes after a terminal status. |
| `WS /api/events?last_seq=N&token=JWT` | Global per-user feed (`run.*`, `agents.status`, `file.changed`, …; `token.delta` excluded). |
| `WS /api/terminal?cols=&rows=&vm_id=&token=JWT` | PTY in the sandbox. Client → `{"type":"stdin","data"}`, `{"type":"resize","cols","rows"}`; server → `ready`, `stdout`, `exit`, `error`. |

Every event: `{"type", "seq", "ts", "run_id", "call_id", "parent_call_id", "data"}` with types
`run.status`, `run.created`, `agent.started|finished|failed`, `subagent.spawned`, `token.delta`, `token.reset`,
`tool.call|result`, `file.changed`, `terminal.output`, `test.result`, `artifact.created`, `decision.made`,
`escalation.requested|resolved`, `vault.written`, `agents.status`, `plan.created`, `error`.

Terminal run statuses (what `app.js` polls for): `done`, `failed`, `escalated`. Non-terminal: `queued`, `planning`,
`running`, `verifying`, `awaiting_user`.
"""


def main() -> None:
    app = create_app()
    spec: dict[str, Any] = app.openapi()
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    lines = ["# Jevora IDE API", "",
             f"Version {spec['info']['version']}. Interactive docs: `/docs` (Swagger) and `/redoc` on the running "
             "server. Machine-readable spec: [openapi.json](openapi.json).", "",
             "Authentication: when `AUTH_REQUIRED=true`, send `Authorization: Bearer <access_token>` (WebSockets: "
             "`?token=`). Tokens come from `/api/auth/login|signup|refresh`; the refresh token is an httpOnly cookie. "
             "With `AUTH_REQUIRED=false` (default) requests act as the local user.", ""]
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            tag = (op.get("tags") or ["other"])[0]
            by_tag.setdefault(tag, []).append((method.upper(), path, op))
    for tag in sorted(by_tag):
        lines += [f"## {tag}", "", "| Method | Path | Summary |", "|---|---|---|"]
        for method, path, op in sorted(by_tag[tag], key=lambda item: (item[1], item[0])):
            summary = (op.get("summary") or op.get("operationId", "")).replace("|", "/")
            doc = (op.get("description") or "").strip().splitlines()
            if doc:
                summary = f"{summary} — {doc[0].replace('|', '/')}"
            lines.append(f"| `{method}` | `{path}` | {summary} |")
        lines.append("")
    lines.append(WEBSOCKETS)
    (docs / "API.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {docs / 'API.md'} and {docs / 'openapi.json'} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
