# Decisions

Choices made while implementing the backend where the spec left room. The fixed decisions from the brief
(§7) are listed first and were applied as given.

## Fixed by the brief

| Topic | Decision |
|---|---|
| Decision engines | Laya primary, Jev optional fallback, RulesEngine always present; Laya runs in **shadow mode** per schema until the promotion gate validates it. |
| Escalation threshold | `0.65` (configurable: `ESCALATION_THRESHOLD`, `PUT /api/decision/config`). |
| Limits | Iteration ceiling `6`, subagent depth `4`, fan-out `5`. |
| Screen control | Isolated from the coder sandbox by default (separate headless browser process, or a Playwright server container with `SCREEN_BROWSER=docker`). |
| Config storage | Providers / router config / decision config live in the local SQLite DB (secrets Fernet-encrypted); the vault holds history only. |
| Web search | Behind `WEB_SEARCH_ENABLED=false`; the UI item stays struck through unless enabled. |
| Auth | Optional locally (`AUTH_REQUIRED=false`, auto-created local user); JWT + refresh cookie + OAuth when enabled. |
| Scope | No multiplayer/team features, no hosted version. |

## Made during implementation

| # | Topic | Decision | Why |
|---|---|---|---|
| D1 | Event-loop compatibility | All subprocesses (sandbox, git, ripgrep, PTY readers) run through thread-backed `subprocess.Popen`; Playwright runs on a dedicated Proactor loop thread. | `uvicorn --reload` on Windows uses a SelectorEventLoop, which cannot spawn asyncio subprocesses. |
| D2 | Run status for escalations | Decision-engine escalations set the run to **`escalated`** (terminal for the UI, resumable). In-run approvals of risky actions use **`awaiting_user`** (non-terminal; the run blocks until answered or `APPROVAL_TIMEOUT_S`). | `app.js` polls until `done/failed/escalated`; an escalation must stop the spinner, while an approval is a short pause. |
| D3 | Cancellation status | Cancelled runs end as `failed` with `cancelled: true` and `error: "cancelled by user"`. | Keeps the three terminal statuses of the contract. |
| D4 | Event `seq` | One global monotonic `seq` for all events; run streams filter by `run_id`. Replay buffers are in memory (20k events/run). | Simple, gap-tolerant replay with `last_seq`; after a restart the stream starts with a status snapshot instead. |
| D5 | Evidence enforcement | `EvidenceItem` carries an HMAC (key in `DATA_DIR/evidence.key`) over kind/source/payload; `revalidate_instances="always"`; `Evidence` rejects strings, `AgentNarrative` and self-report keys. | "Structurally cannot be constructed from agent narrative", and survives checkpoint/restart. |
| D6 | Verification safety rule | If any non-rules engine disagrees with the RulesEngine on `confirm_done`, the decision becomes `escalate`. | A model can never confirm work the evidence rules reject (e.g. failing tests + "all tests pass" claim). |
| D7 | Independent test pass | With the Iterative Tester plugin on, the `tester` node re-runs the suite itself after every coding iteration; the coder's own test runs are evidence too, but never the only check. | Evidence comes from tool execution, not from the agent that wrote the code. |
| D8 | Two model tiers per run | `classify` triages every non-chat task with a small `fast_classification` LLM call (recorded as a `router` agent call); planner/critic use `heavy_reasoning`. | Satisfies "two distinct tiers in the same run" while keeping the routing decision itself non-generative (route_model). |
| D9 | Default category → model mapping | Ranked by tier preference per category, then provider kind (anthropic, openai, google, custom, local), then a curated model preference list. Overridable per category via `PUT /api/router/config`. | Sensible defaults with any mix of providers, including Ollama-only. |
| D10 | UI model aliases | `gpt-4o`, `claude-3-7-sonnet`, `gemini-2-5-pro`, `local` resolve to a saved provider of the matching kind; unavailable aliases are shown as "not configured" in the picker and fall back to Auto with a warning event if sent anyway. | The static picker must map to real bindings. |
| D11 | Duplicate provider names | `409` with `existing_id`; the frontend then updates that provider. | Matches the UI's "save by name" behaviour without duplicates. |
| D12 | Provider keys in the browser | The frontend no longer stores API keys in `localStorage` (only id/name/url). | Keys live encrypted in the backend only. |
| D13 | Ollama autodiscovery | A reachable `OLLAMA_BASE_URL` creates an "Ollama (local)" provider per user once; deleting it records a dismissal so it is not recreated. | Local models are first-class without setup. |
| D14 | Workspace memory | Hybrid retrieval: BM25 (identifier-aware tokenizer) fused with embedding cosine via reciprocal-rank fusion; embeddings come from any embedding-tier model (e.g. Ollama `nomic-embed-text`), else lexical only. | Robust with weak or missing embedding models. |
| D15 | Per-run diff | Snapshot of the workspace at run start (persisted gzip, so resumed runs diff against the true start); unified diff at verification and finalize. | Captures changes made by terminal commands too, not only file tools. |
| D16 | Git safety | Git runs with `core.hooksPath` pointed at the null device and `core.fsmonitor=false`. | Repository hooks would otherwise execute code on the host. |
| D17 | Vault layout | Everything under `<vault>/Jevora/`: run notes, one note per call (wikilinked parent↔child), agent notes, daily notes, an index MOC. Machine data lives in a managed `json` block per note; user text outside `<!-- jevora:begin/end -->` markers is never overwritten. | Readable in Obsidian, portable, and round-trips to the DB (`POST /api/vault/reindex`). |
| D18 | Deleting a run | Removes DB rows, LangGraph checkpoints, the snapshot, and the generated vault notes (managed index/daily lines too). | History deletion should not leave orphaned private data. |
| D19 | Fair scheduling | The executor picks the user with the fewest active runs, ties broken by least-recently-served. | Plain round-robin let a just-served user jump ahead of a newly arrived one. |
| D20 | Approvals across restarts | A previously *approved* identical action (fingerprint) is not asked again after a resume; denials are always re-asked. | Avoids double prompts without silently blocking. |
| D21 | Local models without tool calling | On a 400 for a tool-bearing request the runtime switches that call to a text protocol (`<tool_call>{…}</tool_call>`); bare JSON tool calls in content are also accepted. | Many local servers reject native tools. |
| D22 | Laya fine-tuning | The harness exports data, fits calibration temperatures locally (`laya.calibrate`), and can drive the official Laya RLCD training script; only the eval promotion gate flips `validated=true`. | Full RLCD training lives in the Laya repo's notebooks/scripts. |
| D23 | Seed dataset | A synthetic seed dataset (160 verification + 28 routing items) ships for evaluation; it mirrors the RulesEngine policy, so real validation must include labelled decisions from actual runs (escalation answers + `POST /api/decisions/{id}/label`). | Cold start without circularity claims. |
| D24 | `/test` semantics | `/test` runs the suite; with a URL it also drives the app with the screen agent and checks quoted expectations (`expect "Hello"`). Unit tests are required only if the project has a test framework or no URL was given. | One command for unit and screen tests. |
| D25 | Local sandbox interpreter | `LOCAL_UNSAFE_EXEC` puts the backend's own interpreter first on `PATH`. | Otherwise `python` may resolve to an unrelated interpreter on the host. |
| D26 | OAuth start | `GET /api/auth/oauth/{p}/start?format=json` returns `{url}` (or a 501 JSON when unconfigured) so the page can toast errors; PKCE + signed state cookie. | The UI must show a clear error when OAuth isn't configured. |
| D27 | Branding | User-visible engine names come from `/api/system/health` (`decision_engine.active`: `Laya`, `Jev` or `Rules`). | The old "JEV" strings were hard-coded. |
