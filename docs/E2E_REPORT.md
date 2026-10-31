# End-to-End Verification Report

Date: 2026-10-31 · Machine: Windows 11 build 26300, Python 3.12, Node 25 ·
Database: SQLite (`aiosqlite`) · Decision layer: RulesEngine (+ Laya eval, see below)

This report records what was verified end-to-end on a live instance, and what
remains environment-blocked. It complements [KNOWN_LIMITS.md](KNOWN_LIMITS.md).

## Verified live

### Backend boot & persistence
- Full app boot with SQLite (`DATABASE_URL` override); Alembic migrations
  `0001 → 0005` (incl. `browser_sessions`, durable `events`) applied cleanly.
- `GET /api/system/health` → 200 with `database.ok`, decision engine active.
- Frozen (PyInstaller) backend serves the same health payload.

### Auth
- `POST /api/auth/signup` → 201; `POST /api/auth/login` → 200 (interactive
  session on 127.0.0.1); logout, `/me`, and the email-not-registered path all
  observed live. Password-reset OTP flow covered by the test suite
  (`tests/integration/test_auth.py`).

### Browser control (DCOM / desktop / Playwright)
- API-driven session lifecycle: create (201) → start → navigate (UI served,
  real screenshot PNG captured) → blocked navigation returns `ok=False`
  ("blocked: …") → assertions (`title_contains`, `text_visible`) → stop →
  delete. All 8 REST routes exercised.
- Desktop-capture driver produced real PNGs via BitBlt/`GetDIBits`.
- DCOM path: `InternetExplorer.Application` is registered but its COM server
  is disabled on this Windows build → start() raises a clear
  `BrowserControlError`, mapped to HTTP 503 — correct degradation.

### Desktop shell (Electron)
- Backend process manager (dev + packaged modes) verified headlessly: spawn →
  free-port pick (8000 busy → 8001) → health wait → clean SIGBREAK shutdown.
- Packaged mode against `release/win-unpacked/`: bundled backend healthy
  (200), frontend served (200), resources layout confirmed.
- Artifacts built: `Jevora-IDE-Setup-0.2.0.exe`, `Jevora-IDE-Portable-0.2.0.exe`,
  `latest.yml`. **GUI window launch itself requires an interactive session**
  (the automation shell cannot draw windows) — run `npm run dev` or the
  portable exe to see it.

### New backend features (this session)
- **Durable event replay**: bus events persisted to the `events` table; a
  restarted bus continues the seq counter and replays gap-free
  (`tests/unit/test_events_durable.py`).
- **Per-workspace run serialization**: runs sharing a workspace never overlap
  (their diffs can no longer interleave); different workspaces run in
  parallel (`tests/unit/test_executor_workspace.py`).
- **Sandbox network allow-list**: host-side CONNECT/absolute-HTTP proxy
  enforcing `SANDBOX_NETWORK_ALLOWLIST`, wired into Docker (via
  `host.docker.internal`) and local sandboxes through standard proxy env
  (`tests/unit/test_netfilter.py`, 9 tests incl. live loopback tunnels).

## Test suite status
- Browser-control: 35/35 pass.
- Full suite (excluding e2e): 260+ passed, 12 skipped; the two pre-existing
  failures (`test_history_beyond_twenty…`, `test_risky_command_requires_approval`)
  reproduce on a clean tree and are unrelated (Cygwin `rm` crash; legacy
  history test).

## Environment-blocked (not code gaps)
| Item | Blocker |
|---|---|
| Real-model Build run (llama3.2 via Ollama) | Ollama not installed on this machine |
| Jev decision engine live test | TypeSafe waitlist — no API key |
| Computer-use adapters (OpenAI/Claude/Gemini) live | no provider keys |
| Docker-mode Playwright + DockerSandbox live run | Docker unavailable in this shell |
| Electron GUI window verification | needs an interactive desktop session |
| Auto-update fetch end-to-end | needs the first GitHub release published |

## Decision-layer evaluation
`python -m backend.decision.eval` runs against the seed dataset (188 items);
see `docs/DECISIONS.md` D23 for why real validation needs labelled decisions
from actual runs, and `.jevora-data/eval/report-*.md` for the latest report.
