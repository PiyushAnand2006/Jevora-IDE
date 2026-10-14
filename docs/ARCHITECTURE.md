# Architecture (implementation)

This complements the product-level `architecture.md` at the repository root with how the backend is
actually built.

```
 frontend/ (index.html, app.js, auth.html)  ──HTTP/WS──►  FastAPI (backend/main.py)
                                                              │
   ┌──────────────────────────────────────────────────────────┴───────────────────────────────┐
   │ api/  auth · providers · runs · agents · workspace · terminal · vms · vault · mcp ·        │
   │       attachments · extras(commands, plugins, decision) · system(health, metrics, usage)  │
   └───────┬──────────────────────────┬───────────────────────────┬────────────────────────────┘
           │                          │                           │
   RunService + RunExecutor    ProviderRouter (LiteLLM-style)   EventBus (seq, replay)
   (fair queue, cancel,        adapters: OpenAI-compat,         → WS /api/runs/{id}/stream,
    crash recovery)            Ollama, Anthropic, Gemini;        SSE, WS /api/events
           │                   retries, breaker, fallbacks,
           ▼                   JSON repair, usage/cost
   Orchestrator (LangGraph StateGraph + AsyncSqliteSaver checkpoints)
     classify → planner → coder|ui_designer|custom_agent → tester → critic → verifier ─┬→ finalize
     classify → chat ───────────(changes)──→ tester → … verifier ─────────────────────┤
     classify → researcher|analyst → verifier                                         ├→ worker (retry)
     classify → run_command | critic(/review) | planner(/plan) | tester(/test)          └→ escalate (interrupt)
           │
   AgentRuntime (model ⇄ tool loop, budgets, loop detection, text-tool fallback)
     ├─ ToolRegistry: fs (read/list/search/write/edit/apply_patch/delete), terminal, run_tests, git,
     │   spawn_subagent, web_search (flag), screen_test, MCP tools (mcp__server__tool), plugin tools
     ├─ AgentPool: CallNode tree (cancel cascades), RunBudget, live counters
     ├─ MemoryService: run scratchpad, agent notes, workspace hybrid retrieval
     └─ SandboxManager: DockerSandbox (limits, gVisor, network off) | LocalSandbox (flagged) | VMs | PTY
           │
   DecisionService (ensemble policy) ── Laya (shadow until validated) · Jev (fallback) · Rules (always)
     typed ops: verify_work · route_model · assign_agent · select_next_action · decide_retry · confirm_completion
     every verdict logged with the state + evidence it saw → eval/fine-tune datasets
           │
   VaultService (Obsidian notes, reindex)   ScreenService (computer-use loop, Playwright/desktop drivers)
   MCPManager (stdio/HTTP/SSE sessions)     PluginRegistry (builtin toggles, entry points, manifests)
   SQLite (SQLAlchemy 2 async + Alembic) — users, providers, model bindings, router config, agents,
   runs, agent_calls (parent→child), decisions, artifacts, attachments, MCP servers, plugin state,
   VMs, escalations, llm_calls (tokens/cost), settings
```

## Request → run lifecycle

1. `POST /api/runs` validates the prompt (empty / oversized / mode), parses slash commands, stores the run
   as `queued`, publishes `run.created`, and enqueues it. It returns immediately.
2. The executor picks runs fairly across users (bounded by `MAX_CONCURRENT_RUNS`) and calls
   `Orchestrator.run`. A fresh run snapshots the workspace (for the diff) and invokes the graph. A resumed run
   (`restart` after a crash, an escalation answer, or `POST /resume`) continues from the last checkpoint.
3. Each node updates the run status (`planning → running → verifying → done|failed|escalated`) and emits
   typed events. Agent calls create `agent_calls` rows with `parent_call_id`, depth and lineage.
4. The verifier builds `Evidence` from signed tool output and asks the DecisionService. Depending on the
   verdict it confirms, restarts with feedback, reassigns to a stronger tier, or escalates (LangGraph
   `interrupt`). The iteration ceiling forces an escalation.
5. Finalize writes the diff artifact, composes the output (verdict, tests, files), stores memory, and sets
   the terminal status. A finish hook writes the Obsidian notes and releases the run's sandbox.

## Safety model

* **Paths**: `WorkspaceGuard.resolve` uses `realpath`, so symlink and junction chains are followed before
  the containment check. It also blocks UNC paths, NUL bytes, alternate data streams and Windows device
  names, and forbids writes to `.git/` and `.jevora/`.
* **Permissions**: each tool declares a permission. Agents only see tools they hold, and subagents receive
  the intersection with their parent (`AgentConfig.narrowed`), plus `min` limits.
* **Commands**: `classify_command` marks commands safe, risky or blocked. Blocked commands never run. Risky
  ones follow `APPROVAL_POLICY` (`auto`, `ask_on_risk` or `always_ask`) through an `awaiting_user`
  escalation.
* **Sandbox**: Docker with `--cap-drop ALL`, `no-new-privileges`, CPU, memory and PID limits, network off
  by default, and an optional gVisor runtime. Every exec is wrapped in `timeout` and tagged so a cancel kills
  exactly that process tree.
* **Secrets**: provider keys and MCP env/headers are Fernet-encrypted. The key lives in the OS keyring or a
  0600 file. Keys are never returned by the API, never logged (a redaction filter runs on every log record
  and on provider error text), and the scrubbed sandbox environment does not include them.
* **Decisions**: evidence-only verification, rules veto on `confirm_done`, low-confidence and high-risk
  escalation.

## Extending

* New provider vendor: subclass `ProviderAdapter` (`backend/providers/base.py`) and select it in
  `ProviderService.adapter_for`.
* New decision engine: subclass `DecisionEngine` (`backend/decision/interface.py`) and add it to
  `DecisionService.engine` and the config's `fallbacks`.
* New tool, node hook or run hook: write a plugin (see [PLUGINS.md](PLUGINS.md)).
