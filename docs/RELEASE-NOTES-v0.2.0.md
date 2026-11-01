# Jevora IDE 0.2.0 — first packaged release

The first installable Windows build: a self-contained desktop app (no Python, Node or Docker required) wrapping the full agent IDE.

## Highlights

- **Desktop app** — NSIS installer + portable build; desktop/Start-Menu icons, tray, and automatic updates via GitHub Releases.
- **Agent orchestration with governance** — LangGraph planner → coder → tester → critic → verifier loops; subagents spawned recursively; every subagent's work judged by the decision layer on signed evidence and re-dispatched with feedback until it passes.
- **Auto model routing** — health-aware routing across your saved providers (chat → best working model; build → complexity-classified tiers; analysis → reasoning; research → retrieval), with hard wall-clock deadlines on every call.
- **Sandboxed execution** — Docker containers when available, host execution otherwise; per-sandbox network allow-lists.
- **Browser control** — bundled headless Chromium for automatic UI testing; DCOM and desktop-capture drivers on Windows.
- **Web search** — keyless DuckDuckGo backend by default (Tavily/Brave/SearXNG optional).
- **Lean install** — ~850 MB installed (decision-engine ML stack excluded; Rules engine governs, Laya stays shadow-mode from source checkouts).

## Install

1. Download `Jevora-IDE-Setup-0.2.0.exe` below and run it (SmartScreen: **More info → Run anyway** for the unsigned build).
2. Launch **Jevora IDE** from the desktop icon.
3. Sign in or use local mode, add a model provider in **Context & Tools → Add provider**, and start a task.

## Notes

- First launch performs database migrations and may take a few extra seconds.
- `LOCAL_UNSAFE_EXEC=true` is set by the installer so agents and the terminal run on the host until Docker Desktop is installed.
- Updates: installed builds check GitHub Releases every 6 h and offer **Restart to update**.

## Fixes in this cycle

- Health-aware model routing with per-call deadlines (no more multi-minute hangs or `agent token budget exceeded` on trivial prompts — the acting-brain prompt was carrying a 6,297-line tool catalog on every call).
- Durable event replay across restarts; per-workspace run exclusivity.
- Browser sessions migration + REST error mapping; original Jevora J app icon; Context & Tools panel hidden by default.
