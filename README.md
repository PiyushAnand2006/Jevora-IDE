# Jevora IDE

An agent-native, local-first IDE: a pool of dynamically created agents
(plan → code → test → verify) coordinated by a LangGraph orchestrator and
governed by a non-generative decision layer (**Jevora** — Rules engine, with
Laya as a shadow engine), with sandboxed execution, automatic screen testing
via browser control, an Obsidian vault for portable history, and a Windows
desktop app with auto-update.

## Highlights

- **Agent orchestration** — planner → coder → tester → critic → verifier loops;
  agents spawn subagents recursively, and every subagent's work is judged by the
  decision layer on signed evidence (never self-reports). Unsatisfactory work is
  re-dispatched with evidence-based feedback until it passes, then bounded by
  escalation to the user.
- **Auto model routing** — the router ranks saved provider models by recent
  health and routes per mode: chat → best available model; build → classified
  complexity → reasoning/fast tier; analysis → reasoning; research → retrieval.
  Dead models (404/hang) are demoted automatically; every call has a hard
  wall-clock deadline.
- **Sandboxed execution** — Docker containers (CPU/memory/PID limits, dropped
  capabilities, optional gVisor) with automatic fallback to host execution for
  consumer installs; per-sandbox network allow-lists via a host-side proxy.
- **Browser control** — Playwright headless Chromium sessions and OS-level
  desktop capture for automatic UI testing; bundled with the desktop app.
- **Web search** — keyless DuckDuckGo backend out of the box (Tavily, Brave and
  SearXNG supported), native provider search when the model offers it.
- **Local-first persistence** — SQLite via SQLAlchemy/Alembic; run history
  mirrored to an Obsidian vault as plain markdown.
- **Ships like a coding agent** — runs produce a live plan checklist, an
  auto-commit of verified work on the current branch, and a per-run **Revert**
  (pre-run workspace snapshots); per-run diffs list every changed file as a
  chip that opens in the tabbed workspace editor (Monaco, Ctrl+S, live agent
  edits).
- **Projects & full history** — chats grouped under project folders (ZCode
  style), open existing folders from disk, and build/research/analysis runs
  saved with per-mode badges exactly like chat.
- **Extensibility** — installed **skills** (SKILL.md packages the agent loads
  with `use_skill`), **MCP server tools** callable by the agents, drop-in
  **plugins**, and per-project **custom slash commands**
  (`.jevora/commands/<name>.md`).
- **Project context** — `JEVORA.md` / `AGENTS.md` instructions injected into
  every agent prompt, `@file` mentions inlining workspace files into context,
  and an aider-style **repo map** for large repositories.

## Download (Windows)

Grab `Jevora-IDE-Setup-<version>.exe` from
[GitHub Releases](https://github.com/PiyushAnand2006/Jevora-IDE/releases),
install, and launch from the desktop icon. The installer bundles the backend,
the UI, and a headless Chromium for browser control — no Python, Node or Docker
required. Installed apps check GitHub for updates automatically and offer a
one-click **Restart to update**.

> On machines where a security product blocks Chromium's sandbox broker, the
> shipped shortcuts pass `--no-sandbox` (the installer also sets
> `JEVORA_DISABLE_SANDBOX=1`); renderer isolation is disabled on such installs.

## Quickstart (web, from source)

Requirements: Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows / Git Bash
# .venv/bin/pip install -r requirements-dev.txt       # Linux/macOS

# run the backend + UI (defaults to a local SQLite database in .jevora-data/)
DATABASE_URL="sqlite+aiosqlite:///.jevora-data/jevora.db" \
  .venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 — the UI is served by the backend itself. Without
`AUTH_REQUIRED=true` the app acts as an auto-created local user.

> The non-test default database is PostgreSQL on localhost:5432 (see
> `backend/config.py`); for local development either set `DATABASE_URL` to a
> `sqlite+aiosqlite:///...` URL as above or start the compose stack
> (`docker compose up -d postgres`).

Optional subsystems (see `.env.example` for the full list):

```env
BROWSER_CONTROL_ENABLED=true        # browser-control sessions (DCOM/Playwright/desktop)
SCREEN_ALLOWED_DOMAINS=["localhost","127.0.0.1"]
SANDBOX_NETWORK_ALLOWLIST=["pypi.org","github.com"]   # sandbox egress filter
WEB_SEARCH_ENABLED=true             # default; DuckDuckGo keyless backend
```

## Desktop app from source (Electron)

```bash
npm install
npm run dev          # Electron window; spawns the .venv backend automatically
npm run dist         # NSIS + portable installers into release/
```

Details, packaging, and the release/auto-update workflow:
[docs/ELECTRON.md](docs/ELECTRON.md).

## Tests & checks

```bash
.venv/Scripts/python -m pytest tests -q            # full suite
.venv/Scripts/python -m pytest tests/unit -q       # fast
.venv/Scripts/python -m ruff check backend tests   # lint
.venv/Scripts/python -m backend.decision.eval      # decision-layer eval report
```

Subsystem docs: [API](docs/API.md) · [decisions](docs/DECISIONS.md) ·
[plugins](docs/PLUGINS.md) · [browser control](docs/BROWSER_CONTROL_ARCHITECTURE.md) ·
[desktop packaging](docs/ELECTRON.md) · [email deployment](docs/DEPLOYMENT_EMAIL_SETUP.md) ·
[verification report](docs/E2E_REPORT.md) · [known limits](docs/KNOWN_LIMITS.md).
