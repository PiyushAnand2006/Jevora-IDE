/**
 * Jevora IDE — workspace panels (Library, Scheduled, Projects), run timeline,
 * diff viewer, Monaco editor, MCP catalog, agent export/import, Laya labels.
 *
 * Loaded after app.js; reuses the global `api` helper and `$` from app.js.
 * Everything degrades gracefully when endpoints are unavailable.
 */
(function () {
  "use strict";

  // app.js exposes the API client on window inside its own DOMContentLoaded
  // handler, which runs before the wire() below — so capture it there, not at
  // script-parse time (a parse-time check saw undefined and killed this file).
  let api = null;

  /* ================= shared modal scaffold ================= */

  let overlay = null;
  function openModal(title, build) {
    closeModal();
    overlay = document.createElement("div");
    overlay.className = "jv-modal-overlay";
    const box = document.createElement("div");
    box.className = "jv-modal";
    const head = document.createElement("div");
    head.className = "jv-modal-head";
    head.innerHTML = "<strong></strong>";
    head.querySelector("strong").textContent = title;
    const close = document.createElement("button");
    close.className = "jv-modal-close";
    close.textContent = "×";
    close.addEventListener("click", closeModal);
    head.appendChild(close);
    const body = document.createElement("div");
    body.className = "jv-modal-body";
    overlay.appendChild(box);
    box.appendChild(head);
    box.appendChild(body);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    build(body);
    return body;
  }
  function closeModal() { if (overlay) { overlay.remove(); overlay = null; } }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function tryApi(method, path, body) {
    const m = method.toUpperCase();
    if (m === "GET") return api.get(path);
    if (m === "POST") return api.post(path, body || {});
    if (m === "PUT") return api.put(path, body || {});
    if (m === "PATCH") return api.patch(path, body || {});
    if (m === "DELETE") return api.del(path);
    return api.request(method, path, body);
  }
  function toast(msg) {
    const t = document.querySelector(".toast-container");
    if (!t) return;
    const n = el("div", "toast", msg);
    t.appendChild(n);
    setTimeout(() => n.remove(), 3500);
  }

  /* ================= Library (agent templates) ================= */

  async function openLibrary() {
    openModal("Library — agent templates", async (body) => {
      body.appendChild(el("div", "jv-muted", "Loading templates…"));
      let templates = [];
      try { templates = await tryApi("GET", "/api/agents/templates"); }
      catch (_) { body.lastChild.textContent = "Could not load templates."; return; }
      body.innerHTML = "";
      templates.forEach((t) => {
        const card = el("div", "jv-card");
        card.appendChild(el("div", "jv-card-title", t.name || t.id));
        card.appendChild(el("div", "jv-muted", (t.description || "").slice(0, 160)));
        const row = el("div", "jv-row");
        const use = el("button", "jv-btn", "Use in new task");
        use.addEventListener("click", () => {
          const input = document.querySelector("#chatInput, textarea");
          if (input) { input.value = `[${t.name}] `; input.focus(); }
          closeModal();
        });
        const spawn = el("button", "jv-btn jv-btn-secondary", "Create agent");
        spawn.addEventListener("click", async () => {
          try {
            await tryApi("POST", "/api/agents", { name: t.name, system_prompt: t.system_prompt || "", template: t.id });
            toast(`Agent created from ${t.name}`);
          } catch (e) { toast("Create failed: " + e.message); }
        });
        row.appendChild(use);
        row.appendChild(spawn);
        card.appendChild(row);
        body.appendChild(card);
      });
    });
  }

  /* ================= Scheduled tasks ================= */

  async function openScheduled() {
    openModal("Scheduled tasks", async (body) => {
      const form = el("div", "jv-form");
      form.innerHTML = `
        <input class="jv-input" id="jv-sch-name" placeholder="Task name (e.g. Morning repo review)">
        <textarea class="jv-input" id="jv-sch-prompt" rows="2" placeholder="Prompt to run on schedule"></textarea>
        <div class="jv-row">
          <select class="jv-input" id="jv-sch-mode"><option>chat</option><option>build</option><option>research</option><option>analysis</option></select>
          <input class="jv-input" id="jv-sch-int" type="number" min="1" value="1440" title="Interval in minutes">
        </div>
        <button class="jv-btn" id="jv-sch-add">Schedule</button>`;
      body.appendChild(form);
      const list = el("div", "jv-list");
      body.appendChild(list);

      async function refresh() {
        list.innerHTML = "";
        let tasks = [];
        try { tasks = await tryApi("GET", "/api/scheduled"); }
        catch (_) { list.appendChild(el("div", "jv-muted", "Could not load scheduled tasks.")); return; }
        if (!tasks.length) { list.appendChild(el("div", "jv-muted", "No scheduled tasks yet.")); return; }
        tasks.forEach((t) => {
          const item = el("div", "jv-card");
          item.appendChild(el("div", "jv-card-title", (t.enabled ? "🟢 " : "⏸ ") + t.name));
          item.appendChild(el("div", "jv-muted",
            `every ${t.interval_minutes} min · ${t.mode} · next: ${t.next_run_at ? t.next_run_at.replace("T", " ").slice(0, 16) : "-"}`));
          const row = el("div", "jv-row");
          const run = el("button", "jv-btn", "Run now");
          run.addEventListener("click", async () => {
            try { await tryApi("POST", `/api/scheduled/${t.id}/run`); toast("Task dispatched"); }
            catch (e) { toast("Run failed: " + e.message); }
          });
          const toggle = el("button", "jv-btn jv-btn-secondary", t.enabled ? "Pause" : "Resume");
          toggle.addEventListener("click", async () => {
            try { await tryApi("PATCH", `/api/scheduled/${t.id}`, { enabled: !t.enabled }); refresh(); }
            catch (e) { toast("Update failed: " + e.message); }
          });
          const del = el("button", "jv-btn jv-btn-danger", "Delete");
          del.addEventListener("click", async () => {
            try { await tryApi("DELETE", `/api/scheduled/${t.id}`); refresh(); }
            catch (e) { toast("Delete failed: " + e.message); }
          });
          row.appendChild(run); row.appendChild(toggle); row.appendChild(del);
          item.appendChild(row);
          list.appendChild(item);
        });
      }
      form.querySelector("#jv-sch-add").addEventListener("click", async () => {
        try {
          await tryApi("POST", "/api/scheduled", {
            name: form.querySelector("#jv-sch-name").value,
            prompt: form.querySelector("#jv-sch-prompt").value,
            mode: form.querySelector("#jv-sch-mode").value,
            interval_minutes: Number(form.querySelector("#jv-sch-int").value) || 1440,
          });
          toast("Task scheduled");
          refresh();
        } catch (e) { toast("Schedule failed: " + e.message); }
      });
      await refresh();
    });
  }

  /* ================= Projects (workspaces) ================= */

  const WS_KEY = "jevora_selected_workspace";
  function selectedWorkspace() { return localStorage.getItem(WS_KEY) || ""; }
  function setSelectedWorkspace(id, name) {
    if (id) localStorage.setItem(WS_KEY, id); else localStorage.removeItem(WS_KEY);
    localStorage.setItem("jevora_selected_workspace_name", name || "");
  }

  async function openProjects() {
    openModal("Projects", async (body) => {
      const form = el("div", "jv-form");
      const nameIn = el("input", "jv-input");
      nameIn.placeholder = "New project name";
      nameIn.id = "jv-proj-name";
      form.appendChild(nameIn);
      const add = el("button", "jv-btn", "Create project");
      form.appendChild(add);
      body.appendChild(form);
      const list = el("div", "jv-list");
      body.appendChild(list);
      const sel = el("div", "jv-muted");
      body.appendChild(sel);

      async function refresh() {
        list.innerHTML = "";
        let projects = [];
        try { projects = await tryApi("GET", "/api/projects"); }
        catch (_) { list.appendChild(el("div", "jv-muted", "Could not load projects.")); return; }
        const active = selectedWorkspace();
        sel.textContent = active
          ? "Runs are created in the selected project."
          : "No project selected — runs use the default workspace.";
        projects.forEach((p) => {
          const item = el("div", "jv-card");
          item.appendChild(el("div", "jv-card-title", (p.id === active ? "✓ " : "") + p.name));
          const row = el("div", "jv-row");
          const use = el("button", "jv-btn", "Use for new runs");
          use.addEventListener("click", () => {
            setSelectedWorkspace(p.id, p.name);
            toast(`Project "${p.name}" selected`);
            refresh();
          });
          row.appendChild(use);
          item.appendChild(row);
          list.appendChild(item);
        });
      }
      add.addEventListener("click", async () => {
        try {
          await tryApi("POST", "/api/projects", { name: nameIn.value });
          nameIn.value = "";
          toast("Project created");
          refresh();
        } catch (e) { toast("Create failed: " + e.message); }
      });
      await refresh();
    });
  }

  /* ================= Run timeline (subagent tree + verdicts) ================= */

  function renderTimelineNode(node, container, depth) {
    const item = el("div", "jv-tl-node");
    item.style.marginLeft = Math.min(depth, 6) * 18 + "px";
    const statusIcon = node.status === "done" ? "✓" : node.status === "running" ? "◐" : node.status === "failed" ? "✗" : "○";
    item.appendChild(el("div", "jv-tl-agent",
      `${statusIcon} ${node.agent_name || node.role || "agent"} ` +
      (node.model_used ? `(${node.model_used.split("/").pop()}) ` : "") +
      (node.duration_ms != null ? `· ${Math.round(node.duration_ms / 100) / 10}s` : "")));
    (node.decisions || []).forEach((d) => {
      const v = el("div", "jv-tl-decision " + (d.choice === "confirm_done" || d.choice === "complete" ? "jv-ok" : "jv-warn"));
      v.textContent = `⚖ ${d.engine}: ${d.choice}${d.confidence != null ? ` (${Math.round(d.confidence * 100)}%)` : ""} — ${d.reason || ""}`;
      item.appendChild(v);
      const lbl = el("span", "jv-label-btns");
      const up = el("button", "jv-mini-btn", "👍");
      up.title = "Verdict was correct";
      up.addEventListener("click", () => labelDecision(d.decision_id || d.id, "positive"));
      const down = el("button", "jv-mini-btn", "👎");
      down.title = "Verdict was wrong";
      down.addEventListener("click", () => labelDecision(d.decision_id || d.id, "negative"));
      lbl.appendChild(up); lbl.appendChild(down);
      v.appendChild(lbl);
    });
    container.appendChild(item);
    (node.children || []).forEach((c) => renderTimelineNode(c, container, depth + 1));
  }

  async function labelDecision(decisionId, label) {
    if (!decisionId) return;
    try {
      await tryApi("POST", `/api/decisions/${decisionId}/label`, { label });
      toast("Feedback recorded — feeds Laya fine-tuning");
    } catch (e) { toast("Label failed: " + e.message); }
  }

  async function openTimeline(runId) {
    openModal("Run timeline — agents, subagents & decisions", async (body) => {
      body.appendChild(el("div", "jv-muted", "Loading…"));
      let tree;
      try { tree = await tryApi("GET", `/api/runs/${runId}/tree`); }
      catch (_) { body.lastChild.textContent = "Could not load the run tree."; return; }
      body.innerHTML = "";
      const head = el("div", "jv-muted",
        `${tree.agents} agents · ${tree.subagents} subagents · models: ${(tree.models_used || []).join(", ") || "-"}`);
      body.appendChild(head);
      (tree.roots || []).forEach((r) => renderTimelineNode(r, body, 0));
    });
  }
  window.jevoraOpenTimeline = openTimeline;

  /* ================= Diff viewer ================= */

  window.jevoraOpenDiff = async function (runId) {
    openModal("Changes in this run", async (body) => {
      body.appendChild(el("div", "jv-muted", "Loading diff…"));
      let data;
      try { data = await tryApi("GET", `/api/runs/${runId}/diff`); }
      catch (_) { body.lastChild.textContent = "Could not load the diff."; return; }
      body.innerHTML = "";
      const stats = data.stats || {};
      const changed = stats.files_changed ?? stats.files ?? 0;
      body.appendChild(el("div", "jv-muted",
        `${changed} file(s) changed · +${stats.insertions ?? stats.additions ?? 0} −${stats.deletions ?? 0}`));
      if (changed) {
        // Undo everything this run wrote, back to the pre-run snapshot.
        const revert = el("button", "jv-btn jv-btn-danger", "Revert these changes");
        revert.title = "Restore the workspace to the state captured before this run";
        revert.addEventListener("click", async () => {
          if (!window.confirm("Restore the workspace to the state before this run? Files the run created are deleted; edited files are restored.")) return;
          try {
            const r = await tryApi("POST", `/api/runs/${runId}/revert`);
            toast(`Reverted: ${r.restored.length} restored, ${r.deleted.length} deleted` +
              (r.skipped.length ? `, ${r.skipped.length} skipped (binary/locked)` : ""));
            closeModal();
          } catch (e) { toast("Revert failed: " + e.message); }
        });
        body.appendChild(revert);
      }
      const pre = el("pre", "jv-diff");
      const diff = data.diff || "(no changes)";
      diff.split("\n").forEach((line) => {
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "add"
          : line.startsWith("-") && !line.startsWith("---") ? "del"
          : line.startsWith("@@") ? "hunk" : "";
        pre.appendChild(el("div", "jv-diff-line " + cls, line || " "));
      });
      body.appendChild(pre);
    });
  };

  /* ================= Monaco file editor ================= */

  let monacoLoaded = false;
  function loadMonaco() {
    return new Promise((resolve) => {
      if (window.monaco) { monacoLoaded = true; resolve(true); return; }
      if (monacoLoaded) { resolve(Boolean(window.monaco)); return; }
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js";
      s.onload = () => {
        if (!window.require) { resolve(false); return; }
        window.require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs" } });
        window.require(["vs/editor/editor.main"], () => { monacoLoaded = true; resolve(true); },
          () => resolve(false));
      };
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
      setTimeout(() => resolve(Boolean(window.monaco)), 8000);
    });
  }

  window.jevoraOpenEditor = async function (path) {
    openModal("Edit — " + path, async (body) => {
      body.appendChild(el("div", "jv-muted", "Loading file…"));
      let content = "";
      try { content = await tryApi("GET", `/api/workspace/file?path=${encodeURIComponent(path)}`); }
      catch (e) { body.lastChild.textContent = "Could not read the file: " + e.message; return; }
      body.innerHTML = "";
      const holder = el("div", "jv-editor");
      body.appendChild(holder);
      const save = el("button", "jv-btn", "Save");
      save.disabled = true;
      body.appendChild(save);
      let editor = null, textarea = null;
      const hasMonaco = await loadMonaco();
      if (hasMonaco) {
        editor = window.monaco.editor.create(holder, {
          value: content, automaticLayout: true, minimap: { enabled: false },
          fontSize: 13, theme: "vs-dark",
        });
        editor.onDidChangeModelContent(() => { save.disabled = false; });
      } else {
        textarea = document.createElement("textarea");
        textarea.className = "jv-textarea";
        textarea.value = content;
        textarea.addEventListener("input", () => { save.disabled = false; });
        holder.appendChild(textarea);
      }
      save.addEventListener("click", async () => {
        const value = editor ? editor.getValue() : textarea.value;
        try {
          await tryApi("PUT", "/api/workspace/file", { path, content: value });
          save.disabled = true;
          toast("Saved " + path);
        } catch (e) { toast("Save failed: " + e.message); }
      });
    });
  };

  /* ================= MCP catalog ================= */

  const MCP_CATALOG = [
    { id: "filesystem", name: "Filesystem", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
      description: "Read/write files in the workspace via the Model Context Protocol." },
    { id: "github", name: "GitHub", command: "npx", args: ["-y", "@modelcontextprotocol/server-github"],
      description: "Repos, issues and pull requests via the GitHub API (needs a token env)." },
    { id: "fetch", name: "Fetch", command: "uvx", args: ["mcp-server-fetch"],
      description: "Fetch and parse web pages for the agent." },
    { id: "memory", name: "Memory", command: "npx", args: ["-y", "@modelcontextprotocol/server-memory"],
      description: "Persistent knowledge-graph memory for the agent." },
  ];

  async function openMcpCatalog() {
    openModal("MCP catalog — one-click servers", async (body) => {
      MCP_CATALOG.forEach((srv) => {
        const card = el("div", "jv-card");
        card.appendChild(el("div", "jv-card-title", srv.name));
        card.appendChild(el("div", "jv-muted", srv.description));
        card.appendChild(el("div", "jv-muted jv-code", srv.command + " " + srv.args.join(" ")));
        const row = el("div", "jv-row");
        const add = el("button", "jv-btn", "Add server");
        add.addEventListener("click", async () => {
          try {
            await tryApi("POST", "/api/mcp", { id: srv.id, name: srv.name, command: srv.command, args: srv.args });
            toast(`${srv.name} MCP server added`);
          } catch (e) { toast("Add failed: " + e.message); }
        });
        row.appendChild(add);
        card.appendChild(row);
        body.appendChild(card);
      });
      body.appendChild(el("div", "jv-muted",
        "Servers are stored per user and started when enabled. Requires npx (Node.js) or uvx on the host."));
    });
  }

  /* ================= Agent export / import ================= */

  function downloadJson(name, data) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  window.jevoraExportAgent = async function (agentId) {
    try {
      const agent = await tryApi("GET", `/api/agents/${agentId}`);
      downloadJson(`${(agent.name || "agent").replace(/[^\w.-]+/g, "_")}.json`, agent);
    } catch (e) { toast("Export failed: " + e.message); }
  };

  window.jevoraImportAgent = function (file) {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const cfg = JSON.parse(reader.result);
        await tryApi("POST", "/api/agents", {
          name: cfg.name || "Imported agent",
          system_prompt: cfg.system_prompt || "",
          tool_permissions: cfg.tool_permissions || [],
          model_binding: cfg.model_binding || undefined,
        });
        toast("Agent imported");
        if (typeof loadAgents === "function") loadAgents();
      } catch (e) { toast("Import failed: " + e.message); }
    };
    reader.readAsText(file);
  };

  /* ================= toolbar wiring ================= */

  function bindNavButton(labelText, handler) {
    document.querySelectorAll(".left-nav-item").forEach((btn) => {
      if (btn.querySelector("span") && btn.querySelector("span").textContent.trim() === labelText) {
        btn.addEventListener("click", handler);
      }
    });
  }
  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);
  function wire() {
    if (!window.api) return; // app.js must have booted (same tick order)
    api = window.api;
    bindNavButton("Library", openLibrary);
    bindNavButton("Scheduled", openScheduled);
    // "Projects" lives in the sidebar rendered by app.js (project folders with
    // their chats nested underneath); no modal binding here.
    // MCP catalog button inside the Context & Tools panel
    document.querySelectorAll(".ctx-row").forEach((row) => {
      if (row.textContent.includes("MCP")) {
        const btn = el("button", "jv-btn jv-btn-secondary", "Browse MCP catalog");
        btn.addEventListener("click", openMcpCatalog);
        row.appendChild(btn);
      }
    });
    // agent import lives in the Agent tab (app.js adds an Import button next to
    // export on each agent card); nothing to wire here
  }
})();
