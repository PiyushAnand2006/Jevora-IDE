# Jevora IDE 0.4.0 — a full coding-agent release

Everything from 0.3.0 plus the capabilities that put Jevora in the same loop
as Antigravity, Codex and ZCode: it plans visibly, ships committed work you
can review and revert, extends itself with skills/MCP/plugins, and gained a
real editor.

## New

- **Live plan checklist** — the planner's plan renders as a checklist in the run and ticks off as the decision layer verifies the work.
- **Auto-commit** — verified build runs are committed on the current branch (author "Jevora Agent", only the run's files; `AUTO_COMMIT=false` to disable). A "Committed" chip appears on the run.
- **Revert any run** — every run captures a pre-workspace snapshot; the run's **Changes** modal gains **Revert these changes**: edited files are restored, created files deleted, binary paths reported as skipped.
- **Tabbed workspace editor** — a header toggle switches the center stage to a real editor: Monaco with a file tree, dirty markers, Ctrl+S, Save all. Agent edits stream into open files live; finished runs show clickable chips for every changed file. Textarea fallback when offline.
- **Projects** — ZCode-style sidebar: chats grouped under project folders, standalone chats below. Create projects or **open existing folders** from disk (native picker); runs in a project work in that project's folder; chats can move between projects.
- **History for every mode** — build, research and analysis runs are saved like chat (this was chat-only), with colored per-mode badges in the sidebar and the history drawer.
- **Skills** — SKILL.md packages (global `<data>/skills` or per-project `.jevora/skills`). When a task matches, the agent calls **use_skill** and follows the instructions, including helper scripts. Manage in the new **Skills** panel.
- **MCP tools for agents** — enabled MCP servers' tools are callable by the agents (`mcp__server__tool`); the coder/researcher/chat agents hold `mcp.call` out of the box.
- **Custom slash commands** — `.jevora/commands/<name>.md` in a project defines `/name`; `{{args}}` expands, an optional `mode:` line routes it, and the command menu lists project commands.
- **Project instructions** — `JEVORA.md` (or `AGENTS.md`) at the workspace root is injected into every agent prompt.
- **@-file mentions** — type `@` in the composer to reference workspace files; their contents are inlined into the agent's context (guard-resolved, capped, binary-aware).
- **Repo map** — an aider-style structure + symbol outline (Python, JS/TS family, Go, Rust) injected into build/analysis prompts for large repositories.
- **Background-run switcher** — conversations with runs still in flight appear as pulsing pills; click to jump between running tasks.
- **Sandbox & VMs settings** — WSL/Docker detection with install / update / start / download actions right in Settings.
- **Drop-in plugins** — third-party plugins load from `<data>/plugins` without extra configuration.

## Fixes

- Build/research/analysis history is saved (previously only chat created conversations and stored messages).
- The agent tool-permissions editor, Library, Scheduled and Projects panels actually wire up (the API client was not reachable from their modules).
- WSL/Docker detection endpoints; PTY shells with spaces in their path (pwsh) spawn correctly; the terminal WS no longer errors on such machines.
- Auth: signing up with an already-registered email switches to sign-in with a clear message; unknown emails switch to account creation.
- Taskbar/desktop icon: the Jevora logo is embedded in the exe and the Windows AppUserModelID matches.
- CI: lint-clean async endpoints; flaky cancel test deflaked; release workflow publishes installers on tags.

## Install

Download `Jevora-IDE-Setup-0.4.0.exe` below and run it. The installer bundles
everything — no Python, Node or Docker required. Existing 0.3.0 installs
update automatically (Restart to update).
