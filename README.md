# Jevora IDE

An agent-native, local-first IDE backend: a pool of dynamically created agents
(plan → code → test → verify) coordinated by a LangGraph orchestrator and
checked by a non-generative decision layer, with screen-control for automatic
testing, an Obsidian vault for portable history, and a desktop (Electron)
shell with auto-update.

See [architecture.md](architecture.md) for the design, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for how the backend is actually built, and [docs/](docs/) for subsystem docs.

## Quickstart (web)

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

Enable the optional subsystems via environment (see `.env.example` for the
full list):

```env
BROWSER_CONTROL_ENABLED=true        # browser-control sessions (DCOM/Playwright/desktop)
BROWSER_DCOM_ENABLED=true
BROWSER_DESKTOP_CAPTURE_ENABLED=true
SCREEN_ALLOWED_DOMAINS=["localhost","127.0.0.1"]
SANDBOX_NETWORK_ALLOWLIST=["pypi.org","github.com"]   # sandbox egress filter
```

## Desktop app (Electron)

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
[email deployment](docs/DEPLOYMENT_EMAIL_SETUP.md) ·
[known limits](docs/KNOWN_LIMITS.md).
