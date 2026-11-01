# Jevora IDE 0.3.0 — scheduled agents, editor, timeline

Everything from 0.2.0 plus a major step toward the full IDE experience.

## New

- **Scheduled agents** — schedule any prompt on a recurring interval ("review my repo every morning"). Tasks dispatch automatically, reuse their conversation for continuity, and pause while the conversation is busy. Manage them in the new **Scheduled** panel.
- **Monaco editor** — file preview's **Edit** button now opens a real code editor (Monaco, CDN-loaded with textarea fallback) with save to the workspace.
- **Run timeline** — every run card gains a **Timeline** button: the full agent/subagent tree with models, durations, and Jevora verdicts — plus 👍/👎 labels that feed decision-layer fine-tuning.
- **Changes viewer** — a **Changes** button on each run renders the unified diff of what the agents changed.
- **Projects panel** — create and switch workspaces; new runs are created inside the selected project.
- **Library panel** — browse built-in agent templates, use them in a new task, or turn them into saved agents.
- **MCP catalog** — one-click install of curated MCP servers (Filesystem, GitHub, Fetch, Memory).
- **Agent export/import** — download any agent as JSON and import it back.
- **PWA** — installable web app with manifest and theming.
- **Chat memory compaction** — marathon conversations are trimmed to a context budget instead of growing unbounded.
- **CI/CD** — GitHub Actions run tests on every push and build + publish installers automatically on version tags.

## Fixes

- Health-aware model routing with per-call deadlines (no more multi-minute hangs or budget burn from a 333 KB prompt carrying an embedded tool catalog).

## Install

Download `Jevora-IDE-Setup-0.3.0.exe` below and run it. The installer bundles everything — no Python, Node or Docker required. Existing 0.2.0 installs update automatically (Restart to update).
