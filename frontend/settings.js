/**
 * Jevora IDE — settings & governance UI.
 *
 * Opened from the profile chip: a Settings modal whose first tab is the
 * token/cost usage dashboard (per-day bars, per-model table). Also hosts the
 * per-agent tool-permission editor (checkbox grid over the tool catalog).
 * Pure CSS charts — no chart library.
 */
(function () {
  "use strict";

  if (typeof api !== "function") return;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ================= modal scaffold (shared with panels.js) ================= */

  let overlay = null;
  function openModal(title, build) {
    closeModal();
    overlay = el("div", "jv-modal-overlay");
    const box = el("div", "jv-modal jv-modal-wide");
    const head = el("div", "jv-modal-head");
    head.appendChild(el("strong", null, title));
    const close = el("button", "jv-modal-close", "×");
    close.addEventListener("click", closeModal);
    head.appendChild(close);
    const body = el("div", "jv-modal-body");
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

  function fmt(n) { return (n ?? 0).toLocaleString("en-US"); }
  function fmtCost(v) { return "$" + (v ?? 0).toFixed(4); }

  /* ================= Usage dashboard ================= */

  async function openSettings() {
    openModal("Settings — usage & cost", async (body) => {
      body.appendChild(el("div", "jv-muted", "Loading usage…"));
      let daily, byModel;
      try {
        daily = await tryGet("/api/usage?days=14&group_by=day");
        byModel = await tryGet("/api/usage?days=14&group_by=model");
      } catch (e) { body.lastChild.textContent = "Could not load usage: " + e.message; return; }
      body.innerHTML = "";

      const totals = daily.totals || {};
      const row = el("div", "jv-usage-totals");
      row.appendChild(usageStat("Calls", fmt(totals.calls)));
      row.appendChild(usageStat("Tokens", fmt(totals.tokens)));
      row.appendChild(usageStat("Cost (14d)", fmtCost(totals.cost_usd)));
      row.appendChild(usageStat("Errors", fmt((daily.groups || []).reduce((s, g) => s + (g.errors || 0), 0))));
      body.appendChild(row);

      body.appendChild(el("div", "jv-section-title", "Tokens per day (last 14 days)"));
      const chart = el("div", "jv-chart");
      const groups = daily.groups || [];
      const peak = Math.max(1, ...groups.map((g) => (g.prompt_tokens || 0) + (g.completion_tokens || 0)));
      groups.forEach((g) => {
        const col = el("div", "jv-chart-col");
        const tok = (g.prompt_tokens || 0) + (g.completion_tokens || 0);
        const bar = el("div", "jv-chart-bar");
        bar.style.height = Math.max(2, Math.round(tok / peak * 100)) + "px";
        bar.title = `${g.key}: ${fmt(tok)} tokens · ${fmtCost(g.cost_usd)} · ${g.calls} calls`;
        col.appendChild(bar);
        col.appendChild(el("div", "jv-chart-label", String(g.key).slice(5)));
        chart.appendChild(col);
      });
      body.appendChild(chart);

      body.appendChild(el("div", "jv-section-title", "By model"));
      const table = el("table", "jv-table");
      table.innerHTML = "<thead><tr><th>Model</th><th>Calls</th><th>Prompt tok</th>" +
        "<th>Completion tok</th><th>Cost</th><th>Errors</th><th>Avg latency</th></tr></thead>";
      const tbody = el("tbody");
      (byModel.groups || []).forEach((m) => {
        const tr = el("tr");
        [m.key, fmt(m.calls), fmt(m.prompt_tokens), fmt(m.completion_tokens),
         fmtCost(m.cost_usd), fmt(m.errors), Math.round(m.avg_latency_ms || 0) + " ms"]
          .forEach((v) => tr.appendChild(el("td", null, String(v))));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      body.appendChild(table);
      if (!(byModel.groups || []).length) body.appendChild(el("div", "jv-muted", "No model calls in this period."));
    });
  }

  function usageStat(label, value) {
    const s = el("div", "jv-usage-stat");
    s.appendChild(el("div", "jv-usage-value", value));
    s.appendChild(el("div", "jv-usage-label", label));
    return s;
  }

  async function tryGet(path) {
    const r = await api.get(path);
    return r;
  }

  /* ================= Agent permissions editor ================= */

  let toolCatalog = null;
  async function getTools() {
    if (toolCatalog) return toolCatalog;
    try { toolCatalog = await api.get("/api/tools"); } catch (_) { toolCatalog = []; }
    return toolCatalog;
  }

  function groupByPermission(tools) {
    const groups = {};
    tools.forEach((t) => {
      const prefix = (t.permission || "other").split(".")[0];
      (groups[prefix] = groups[prefix] || []).push(t);
    });
    return Object.entries(groups).sort((a, b) => a[0].localeCompare(b[0]));
  }

  window.jevoraEditPermissions = async function (agentId, agentName, current) {
    const tools = await getTools();
    const granted = new Set(current || []);
    openModal(`Permissions — ${agentName}`, (body) => {
      body.appendChild(el("div", "jv-muted",
        "Agents only see tools whose permission they hold; subagents inherit the intersection with their parent."));
      const grid = el("div", "jv-perm-grid");
      groupByPermission(tools).forEach(([prefix, items]) => {
        const group = el("div", "jv-perm-group");
        group.appendChild(el("div", "jv-perm-group-title", prefix));
        items.forEach((t) => {
          const label = el("label", "jv-perm-item");
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = granted.has(t.permission) || granted.has(t.name);
          cb.dataset.permission = t.permission;
          cb.dataset.tool = t.name;
          label.appendChild(cb);
          label.appendChild(el("span", null, t.name));
          label.title = t.description || "";
          group.appendChild(label);
        });
        grid.appendChild(group);
      });
      body.appendChild(grid);
      const save = el("button", "jv-btn", "Save permissions");
      save.style.marginTop = "10px";
      save.addEventListener("click", async () => {
        const checked = [...grid.querySelectorAll("input:checked")];
        // a permission is granted when any of its tools is checked
        const perms = [...new Set(checked.map((c) => c.dataset.permission))];
        try {
          await api.patch(`/api/agents/${agentId}`, { tool_permissions: perms });
          toast("Permissions saved");
          closeModal();
          if (typeof loadAgents === "function") loadAgents();
        } catch (e) { toast("Save failed: " + e.message); }
      });
      body.appendChild(save);
    });
  };

  function toast(msg) {
    const t = document.querySelector(".toast-container");
    if (!t) return;
    const n = el("div", "toast", msg);
    t.appendChild(n);
    setTimeout(() => n.remove(), 3500);
  }

  /* ================= integration: profile menu + agent list ================= */

  function wire() {
    // Settings entry in the profile menu: app.js rebuilds the menu each click,
    // so intercept at the container level via a MutationObserver-free approach:
    document.addEventListener("click", (e) => {
      const menu = document.querySelector(".profile-menu");
      if (!menu || menu.querySelector(".pm-settings") || !menu.contains(e.target) && e.target.id !== "profileChip"
          && !menu.contains(e.target)) return;
      if (menu.querySelector(".pm-settings")) return;
      const settings = el("button", "chip-btn pm-settings", "Settings & usage");
      settings.addEventListener("click", (ev) => {
        ev.stopPropagation();
        menu.remove();
        openSettings();
      });
      const signout = menu.querySelector("button");
      menu.insertBefore(settings, signout);
    }, true);

    // Permissions buttons on agent list items (delegated; survives re-renders)
    const agentList = document.getElementById("agentList");
    if (agentList) {
      agentList.addEventListener("click", (e) => {
        const btn = e.target.closest(".jv-perm-edit");
        if (!btn) return;
        const item = btn.closest("[data-agent-id]");
        if (!item) return;
        const id = item.getAttribute("data-agent-id");
        const name = (item.textContent || "").trim().split("\n")[0].slice(0, 40);
        let perms = [];
        try { perms = JSON.parse(item.getAttribute("data-perms") || "[]"); } catch (_) {}
        window.jevoraEditPermissions(id, name, perms);
      });
    }
  }

  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);

  /* ================= styles ================= */

  const css = document.createElement("style");
  css.textContent =
    ".jv-modal-wide{width:min(760px,94vw)}" +
    ".jv-usage-totals{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}" +
    ".jv-usage-stat{border:1px solid #e6e6ee;border-radius:10px;padding:10px;text-align:center}" +
    ".jv-usage-value{font-size:18px;font-weight:700}" +
    ".jv-usage-label{font-size:11px;color:#7a7a88;text-transform:uppercase;letter-spacing:.5px}" +
    ".jv-section-title{font-weight:700;font-size:12px;color:#4b5563;text-transform:uppercase;" +
    "letter-spacing:.5px;margin:14px 0 6px}" +
    ".jv-chart{display:flex;align-items:flex-end;gap:6px;height:110px;padding:6px 2px 0;" +
    "border-bottom:1px solid #e6e6ee}" +
    ".jv-chart-col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}" +
    ".jv-chart-bar{width:70%;background:linear-gradient(180deg,#6366f1,#312e81);border-radius:4px 4px 0 0;min-height:2px}" +
    ".jv-chart-label{font-size:9px;color:#7a7a88;margin-top:4px;white-space:nowrap}" +
    ".jv-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px}" +
    ".jv-table th{text-align:left;padding:6px 8px;color:#7a7a88;font-weight:600;border-bottom:1px solid #e6e6ee}" +
    ".jv-table td{padding:6px 8px;border-bottom:1px solid #f0f0f5}" +
    ".jv-perm-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;max-height:48vh;overflow-y:auto}" +
    ".jv-perm-group{border:1px solid #e6e6ee;border-radius:10px;padding:8px 10px}" +
    ".jv-perm-group-title{font-weight:700;font-size:11px;text-transform:uppercase;color:#4b5563;margin-bottom:4px}" +
    ".jv-perm-item{display:flex;align-items:center;gap:6px;font-size:12px;padding:2px 0;cursor:pointer}" +
    ".pm-settings{display:block;width:100%;text-align:left;margin-bottom:6px}";
  document.head.appendChild(css);
})();
