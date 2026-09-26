# IDE Coder — Architecture

## 1. System overview

```
IDE Client (desktop / mobile)
        │
        ▼
   Orchestrator  ──────────────────────────────┐
   (LangGraph state graph)                      │
        │                                        │
        ├──► Agent Pool ──► calls subagents ──► (recursive, tracked)
        │       │                                │
        │       ▼                                │
        │   System & Tool Access                 │
        │   (files, terminal, execution sandbox,  │
        │    screen-control/testing agent)        │
        │       │                                │
        ▼       ▼                                ▼
   Decision Layer (Laya / Jev) ◄──────── evidence (logs, screenshots,
        │   confirm / restart / reassign          test results, diffs)
        │   / escalate  +  model routing
        ▼
   Output to user  +  run history → Obsidian vault (local)
```

## 2. Components

### 2.1 Orchestrator — LangGraph
- Modeled as a state graph, not a fixed pipeline: nodes for planner,
  coder, tester, critic, UI-designer, researcher; edges conditioned on
  state (tests passed? confidence above threshold? user confirmation
  needed?).
- Shared state object (scratchpad) carries structured artifacts
  (diffs, test results, specs) between agents — not just chat history.
- Hard iteration ceiling per task to prevent unbounded retry loops.

### 2.2 Agent pool
- Each agent = config object: system prompt + tool permissions + model
  binding + memory scope, instantiated from a template.
- Agents call subagents recursively. Every call is logged with its
  parent agent ID so the UI can render "called by which agent."
- A model router (LiteLLM-style abstraction) sits in front so swapping
  an agent's backing LLM is config, not code.

### 2.3 Decision layer

The decision layer is exposed through a **model-agnostic Decision
Engine interface** — the orchestrator depends on that interface, never
on a specific model. Any implementation receives the current state
plus typed questions and returns a structured, typed decision with
confidence information, and must not generate code or user-facing
prose. This keeps Laya, Jev, or any future decision model swappable
without touching the LangGraph orchestration layer.

Both current candidates are non-generative "System One" models: given
a state + typed questions, they return a typed answer with a
calibrated confidence score in one forward pass — no free text, so no
text hallucination.

| | **Jev** (TypeSafe AI) | **Laya** (Convai Innovations) |
|---|---|---|
| License / hosting | Closed, cloud API | Apache 2.0, self-hosted / runs in-browser (WebGPU/CoreML) |
| Cost | ~$0.042 / 1M input tokens | $0 (you host it) |
| Latency | ~236–276ms | ~33ms (down to single-digit ms on optimized runtimes) |
| Size | Not published | 421M parameters |
| Out-of-the-box accuracy | Reported ~68% on TypeSafe's own benchmark | **Near-random zero-shot** (~36%, below the majority-class baseline) — headline accuracy (~77–84%) only holds *after fine-tuning* on your own task |
| Availability | Waitlist | Public, `pip install laya`, weights on Hugging Face |

**Initial implementation: Laya as the primary Decision Engine, with
Jev as an optional fallback implementation** — not hard-coded, just
the first choice, because Laya fits the local-first, self-hosted
direction of the rest of the system (Obsidian vault, local
models-as-providers) and removes per-decision cost. The catch that
matters most: Laya's strong numbers are *post-fine-tune*, not
zero-shot. Do not wire it into the confirm/restart/reassign loop
straight from the base checkpoint.

Plan:
1. Start by logging every routing/verification decision (state seen +
   outcome) — this becomes both evaluation and fine-tuning data.
2. Evaluate Laya against the project's own confirm/restart/reassign/
   escalate and model-routing schemas, then fine-tune where required
   before relying on it for unattended decisions.
3. Keep Jev, or another validated implementation, as a fallback for
   low-confidence or high-risk Laya decisions until the local engine
   is validated.
4. Keep the Decision Engine interface stable so additional models can
   be added later without modifying the agent orchestrator.

**Decision schemas** (unchanged from earlier design):
- Work verification: `Choice[confirm_done, restart_same_agent,
  reassign_new_agent, escalate]`, fed *evidence* (test/lint output,
  diffs, execution logs) — never the agent's own self-report.
- Model routing: `Choice[heavy_reasoning, fast_classification,
  code_generation, creative_ui, research_retrieval]`, fed the task
  spec before an agent starts; category → model mapping lives in the
  router config, not in the decision model itself.

### 2.4 System & tool access
- File I/O, terminal, execution sandbox (Docker/gVisor) available to
  agents per their configured permissions.
- **Screen-control / testing agent**: a separate agent built on a
  **Computer Use model** — a model trained specifically to control a
  screen via a screenshot-in, coordinate-action-out loop (click at
  x,y; type; scroll), rather than general-purpose vision-language
  reasoning. This is literally what Antigravity's browser subagent
  runs on: Google's **Gemini 2.5 Computer Use** model, kept separate
  from the Gemini 3 Pro model that does the planning/coding. The same
  model *category* exists from multiple providers — Anthropic's
  **Claude Computer Use** and OpenAI's **computer-use-preview**
  (Operator) are direct equivalents — so this agent's model binding
  can go through the same provider/router system as every other
  agent (§2.5) rather than being hardcoded to one vendor.
  It drives Playwright (web) or OS-level automation (desktop apps)
  and produces evidence (screenshots, click logs, pass/fail signals);
  the decision layer judges that evidence, not the agent's own
  narrative of what it did.

### 2.5 Provider management
- Saved providers (P1, P2, …), each either a hosted API or a local
  model.
- Custom provider = Base URL + API key, for any OpenAI-compatible or
  self-hosted endpoint.
- Provider list view for browsing beyond the user's saved set.

### 2.6 Local storage — Obsidian vault
- Agent runs, subagent call chains, and decisions are written as
  markdown notes to a local Obsidian vault rather than only living in
  app state.
- Suggested per-run note structure: task description, agent/subagent
  tree (parent → child), model used per step, decision-layer verdicts
  with confidence, final output/diff, timestamps. This is what makes
  "called by which agent" inspectable outside the app, and doubles as
  the training log for decision-layer fine-tuning (§2.3).

## 3. Client / UI layer
- Desktop: window-chrome shell, top bar (title/history, mode tabs,
  VMs/agent-count/profile), agent output + input column, right
  Context & Tools sidebar. Prototyped in Figma, frame `8:2`.
- Mobile: same content, vertically stacked with a segmented mode
  control. Figma frame `3:2`.
- Both are static prototypes today — no state wiring yet.

## 4. Tech stack (proposed)

| Layer | Choice |
|---|---|
| Orchestration | LangGraph |
| Tool/retrieval glue | LangChain |
| Decision layer | Model-agnostic Decision Engine interface (Laya primary, Jev optional fallback) |
| Model routing | LiteLLM-style abstraction |
| Execution sandbox | Docker / gVisor |
| Screen-control agent | Computer Use model (Gemini 2.5 Computer Use / Claude Computer Use / OpenAI computer-use-preview) driving Playwright (web) or OS-level automation (desktop) |
| Backend | FastAPI + WebSockets (streaming) |
| Local persistence | Obsidian vault (markdown files) |
| Editor surface | Monaco or CodeMirror |
| Client | Desktop-first (framework TBD), mobile companion |

## 5. Open architecture questions

- Where does the Obsidian vault live relative to the app's own
  working directory, and does the user pick the vault path or is one
  created on first launch?
- Confidence threshold below which the decision layer escalates to
  the user, for both Jev and Laya paths.
- Whether the screen-control/testing agent shares the same sandbox as
  the coder agent or runs isolated.
- How saved providers and the model router's category→model mapping
  are persisted (same vault, or separate local config?).
