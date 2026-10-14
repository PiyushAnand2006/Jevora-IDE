# Known limits

* **Laya is not validated.** The engine is integrated, but it starts in shadow mode. `laya` and CPU torch are now installed in `.venv`, but no run against the real Hugging Face weights was done in this session; tests use stand-in backends. Run `python -m backend.decision.eval --promote` on your own labelled decisions before trusting it.
* **Seed dataset is circular.** The synthetic eval set mirrors the RulesEngine policy (DECISIONS D23). Real validation needs labelled decisions from actual runs.
* **Jev is untested live.** It is waitlisted, and its request format is modelled on Laya's `system_one`. It is tested with a mock transport only.
* **Native computer-use adapters** (OpenAI, Claude, Gemini) are tested against recorded API shapes, not live keys. The generic vision-JSON adapter is tested end to end. Docker-mode Playwright (`SCREEN_BROWSER=docker`) is implemented but untested.
* **Real local model quality.** The full flow is verified with a scripted mock LLM. A real Build run with `llama3.2` (3B) through the UI was started for visual checking but not completed or measured; small local models may struggle with tool calling.
* **Event replay** buffers are in memory. After a restart, run streams begin with a status snapshot rather than full history; run state itself is durable (DB + checkpoints).
* **Network allow-list:** the sandbox network is an on/off toggle per sandbox/VM. Domain allow-lists apply to screen control and web tools, not to raw sockets inside the sandbox.
* **Concurrent runs in one workspace** can see each other's changes in their per-run diffs.
* **Desktop control** (pyautogui) is opt-in and untested in CI. DOM assertions are unsupported there.
* **Frontend:** the Library, Scheduled and Projects nav items have no backend features (out of the spec's scope). xterm.js loads from a CDN.
* **Not finished:** `README.md` quickstart and `docs/E2E_REPORT.md` were not written, and a final full-suite rerun after the last frontend tweaks was not done (see the session summary).
