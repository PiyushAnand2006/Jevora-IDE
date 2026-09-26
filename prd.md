# IDE Coder — Product Requirements Document

## 1. Vision

An independent, agent-native IDE for desktop and mobile. It codes,
tests, fixes bugs, and designs UI through a pool of dynamically
created agents — each bound to whatever LLM fits its job — coordinated
by an orchestrator and checked by a fast, non-generative decision
layer that keeps the system from confidently shipping wrong output.

Four working modes: **Build**, **Chat**, **General** (research), and
**Analysis**.

## 2. Who it's for

Developers who want an IDE where they don't drive every agent by
hand — they describe the work, the system assigns the right agent(s)
and model(s), verifies the result, and only surfaces a decision to the
user when the system itself isn't confident.

## 3. Core features

### 3.1 Agent system
- Create an agent for any task, bound to any LLM (config object:
  system prompt + tool permissions + model binding + memory scope).
- Agents can call **subagents**, recursively, to break work down.
- Every run is traceable: the UI must show **which agent called which
  subagent** (parent/child chain), not just a flat task list.
- A live status view shows *n agents / n subagents running*.

### 3.2 Decision layer (verification & routing)
- A typed, non-generative "System One" decision model sits between
  agent steps and decides: confirm work done / restart same agent /
  reassign to a new agent / escalate to the user.
- The same decision layer **auto-assigns models to agents** by task
  type (e.g. heavy-reasoning vs. fast/cheap), based on a small fixed
  taxonomy, not free-form judgment.
- The decision layer must be implemented behind a **model-agnostic
  Decision Engine interface**, so the IDE is not coupled to any single
  decision model. Each implementation exposes the same operations for
  verification, next-action selection, agent assignment, retry decisions,
  completion confirmation, and model routing.
- **Laya** is the primary local Decision Engine implementation for the
  project because it is open-source and self-hostable. **Jev** remains an
  optional Decision Engine implementation/fallback, and additional decision
  models can be added later without changing the orchestration layer.
- Candidate implementations must be evaluated on decision latency,
  verification accuracy, routing quality, incorrect-completion detection,
  and reliability on the project's own decision schemas before being trusted
  for unattended execution. See `architecture.md` §4 for the current
  implementation details and fine-tuning requirements.

### 3.3 Model / provider management
- **Saved providers** list (P1, P2, …) with an "Add provider" action.
- **Custom provider** support: Base URL + API key fields, for any
  OpenAI-compatible or self-hosted endpoint.
- A browsable **provider list** beyond the user's own saved set.
- Local models supported as a provider type (not just hosted APIs).

### 3.4 Build/Chat/General/Analysis workspace
- Mode selector (segmented control on mobile, inline tabs on desktop).
- Context & Tools panel: File preview, Terminal, MCP, Plugins.
- Chat input with an attach menu: Files, Photos, Web search (**not
  yet available** — shown disabled), Commands.
- Agent output canvas (streamed responses, build logs, etc. — content
  format still undesigned, see Open Questions).

### 3.5 Automatic testing (screen control)
- An execution/control agent (vision-capable, drives Playwright/OS
  automation) opens apps, clicks, fills forms, takes screenshots —
  similar in spirit to Google Antigravity's browser agent.
- It produces evidence (screenshots, logs, pass/fail signals); the
  decision layer judges that evidence, not the coder-agent's own
  self-report of what it did.

### 3.6 Local persistence
- Agent work and call history are saved **locally through an Obsidian
  vault** (markdown-based), not only in-app state — so a user's agent
  history is inspectable/portable as plain notes.

### 3.7 Platforms
- Desktop app (primary) and mobile app. Both prototyped in Figma
  (`IDE Coder - UI Prototype` file): mobile frame `3:2`, desktop frame
  `8:2`.

## 4. Non-goals (for now)

- Multiplayer/team collaboration features.
- A hosted/cloud version of the IDE itself (local-first is the
  starting point).
- Full parity with any single competitor (Cursor, Antigravity, Devin)
  — see the earlier scope discussion: ship one working agent + one
  verified end-to-end task before widening.

## 5. Open questions / pending decisions

1. **Auth page** — the original sketch's "sign in" / "webpage opens"
   concepts were crossed out by the user in favor of a design
   referenced from an Instagram reel. **The reel's content isn't
   available yet** (Instagram blocks automated fetching) — needs a
   description, screen recording, or screenshots from the user before
   this can be speced or prompted.
2. Whether tool rows (File preview/Terminal/MCP/Plugins) open inline
   or as separate panels.
3. Sidebar collapse/expand behavior on desktop for narrower windows.
4. What the Agent Output canvas actually renders per mode (chat
   bubbles vs. build log vs. diff view).
5. Final choice between Jev and Laya (or both, split by use case) for
   the decision layer — pending the fine-tuning work described in
   `architecture.md`.

## 6. Success criteria (v1 / MVP)

- One agent type (coder) can take a described task, plan it, execute
  it with real tool access, and have its work verified by the
  decision layer end-to-end.
- Model routing correctly assigns at least two distinct model tiers
  (e.g. fast/cheap vs. high-reasoning) based on task type.
- Agent + subagent call history is written to and readable from the
  local Obsidian vault.
- Desktop screen (Build mode) matches the current Figma prototype and
  is wired to real state, not static mockup data.
