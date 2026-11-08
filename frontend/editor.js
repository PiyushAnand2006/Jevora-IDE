/**
 * Jevora IDE — workspace editor: a tabbed Monaco editor over the chat stage
 * (Antigravity-style). Files open here from run chips, the file drawer, and
 * the file tree; Ctrl+S saves through the workspace API; a textarea fallback
 * covers offline CDN failures.
 *
 * Loaded last; overrides panels.js's single-file modal jevoraOpenEditor.
 */
(function () {
  "use strict";

  const MONACO_CDN = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min";

  function main() {
    if (!window.api) return; // app.js must have booted (same tick order)

  const api = window.api;

  const host = document.getElementById("editorHost");
  const tabsEl = document.getElementById("editorTabs");
  const treeHost = document.getElementById("editorTreeHost");
  const workspace = document.getElementById("editorWorkspace");
  const toggleBtn = document.getElementById("editorToggleBtn");
  if (!host || !tabsEl || !treeHost || !workspace || !toggleBtn) return;

  const state = { tabs: new Map(), active: null, monaco: null, editor: null, treeLoaded: false, ready: null,
                  workspace: null };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ---------------- monaco loading (textarea fallback) ---------------- */
  function loadMonaco() {
    if (state.ready) return state.ready;
    state.ready = new Promise((resolve) => {
      if (window.monaco) { resolve(true); return; }
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = MONACO_CDN + "/min/vs/editor/editor.main.css";
      document.head.appendChild(link);
      const loader = document.createElement("script");
      loader.src = MONACO_CDN + "/min/vs/loader.js";
      loader.onload = () => {
        if (!window.require) { resolve(false); return; }
        window.require.config({ paths: { vs: MONACO_CDN + "/min/vs" } });
        window.require(["vs/editor/editor.main"], () => resolve(Boolean(window.monaco)), () => resolve(false));
      };
      loader.onerror = () => resolve(false);
      document.head.appendChild(loader);
      setTimeout(() => resolve(Boolean(window.monaco)), 12000);
    });
    return state.ready;
  }

  function ensureEditorInstance() {
    if (state.editor || !state.monaco) return state.editor;
    state.editor = state.monaco.editor.create(host, {
      model: null, automaticLayout: true, minimap: { enabled: true }, fontSize: 13,
      theme: "vs-dark", scrollBeyondLastLine: false, tabSize: 2,
    });
    state.editor.addCommand(state.monaco.KeyMod.CtrlCmd | state.monaco.KeyCode.KeyS, () => saveActive());
    return state.editor;
  }

  /* ---------------- tabs ---------------- */
  function renderTabs() {
    tabsEl.innerHTML = "";
    for (const [path, tab] of state.tabs) {
      const tabEl = el("button", "editor-tab" + (path === state.active ? " active" : ""));
      tabEl.type = "button";
      tabEl.title = path;
      tabEl.appendChild(el("span", "editor-tab-name" + (tab.dirty ? " dirty" : ""), tab.label));
      const close = el("span", "editor-tab-close", "×");
      close.addEventListener("click", (e) => { e.stopPropagation(); closeTab(path); });
      tabEl.appendChild(close);
      tabEl.addEventListener("click", () => activate(path));
      tabsEl.appendChild(tabEl);
    }
    const actions = el("div", "editor-tab-actions");
    const saveAll = el("button", "editor-tab-action", "Save all");
    saveAll.type = "button";
    saveAll.addEventListener("click", () => { for (const [path, tab] of state.tabs) if (tab.dirty) saveTab(path); });
    const closeAll = el("button", "editor-tab-action", "Close all");
    closeAll.type = "button";
    closeAll.addEventListener("click", () => { for (const path of [...state.tabs.keys()]) closeTab(path); });
    actions.appendChild(saveAll);
    actions.appendChild(closeAll);
    tabsEl.appendChild(actions);
  }

  function activate(key) {
    const tab = state.tabs.get(key);
    if (!tab) return;
    state.active = key;
    if (tab.textarea) {
      host.querySelectorAll(".editor-fallback").forEach((n) => n.remove());
      host.appendChild(tab.textarea);
      tab.textarea.style.display = "";
      for (const [, other] of state.tabs) if (other !== tab && other.textarea) other.textarea.style.display = "none";
    } else if (state.editor && state.monaco) {
      state.editor.setModel(tab.model);
      state.editor.focus();
    }
    renderTabs();
  }

  async function openTab(path, workspaceId) {
    if (workspaceId !== undefined) state.workspace = workspaceId;
    const wsParam = state.workspace ? `&workspace=${encodeURIComponent(state.workspace)}` : "";
    let tab = state.tabs.get(path + wsParam);
    const tabKey = path + wsParam;
    if (!tab) {
      let file;
      try { file = await api.get(`/api/workspace/file?path=${encodeURIComponent(path)}${wsParam}`); }
      catch (e) { toast(`Could not open ${path}: ${e.message}`); return; }
      if (file.binary) { toast(`${path} is binary — use the file drawer to download it`); return; }
      tab = { original: file.content, dirty: false, label: path.split("/").pop(), textarea: null, model: null,
              path, ws: state.workspace };
      state.tabs.set(tabKey, tab);
      const hasMonaco = await loadMonaco();
      if (hasMonaco) {
        state.monaco = window.monaco;
        ensureEditorInstance();
        tab.model = state.monaco.editor.createModel(
          file.content, undefined, state.monaco.Uri.file("/ws/" + path));
        tab.model.onDidChangeModelContent(() => {
          tab.dirty = tab.model.getValue() !== tab.original;
          renderTabs();
        });
      } else {
        const area = el("textarea", "editor-fallback");
        area.value = file.content;
        area.spellcheck = false;
        area.addEventListener("input", () => { tab.dirty = area.value !== tab.original; renderTabs(); });
        tab.textarea = area;
        host.appendChild(area);
      }
    }
    showWorkspace();
    activate(tabKey);
  }

  async function saveTab(key) {
    const tab = state.tabs.get(key);
    if (!tab || !tab.dirty) return;
    const value = tab.textarea ? tab.textarea.value : tab.model.getValue();
    try {
      await api.put("/api/workspace/file", { path: tab.path, content: value, workspace: tab.ws || undefined });
      tab.original = value;
      tab.dirty = false;
      renderTabs();
      toast(`Saved ${path}`);
    } catch (e) { toast(`Save failed: ${e.message}`); }
  }

  function saveActive() { if (state.active) saveTab(state.active); }

  function closeTab(key) {
    const tab = state.tabs.get(key);
    if (!tab) return;
    if (tab.dirty && !window.confirm(`${tab.path} has unsaved changes. Close anyway?`)) return;
    if (tab.model) tab.model.dispose();
    if (tab.textarea) tab.textarea.remove();
    state.tabs.delete(key);
    if (state.active === key) {
      state.active = null;
      const next = state.tabs.keys().next();
      if (!next.done) activate(next.value);
      else if (state.editor) state.editor.setModel(null);
    }
    renderTabs();
  }

  /* ---------------- file tree ---------------- */
  async function renderTree() {
    let tree;
    try { tree = await api.get("/api/workspace/tree?depth=8&limit=2000"); }
    catch (e) { treeHost.innerHTML = ""; return; }
    treeHost.innerHTML = "";
    treeHost.appendChild(el("div", "editor-tree-title", "FILES"));

    const render = (node, container) => {
      if (node.type === "file") {
        const item = el("button", "editor-tree-file", node.name);
        item.type = "button";
        item.title = node.path;
        if ((node.path + (state.tabs.get(node.path)?.ws ? "" : "")) === state.active) item.classList.add("active");
        item.addEventListener("click", () => openTab(node.path));
        container.appendChild(item);
        return;
      }
      const details = document.createElement("details");
      details.open = node.path === ".";
      const summary = el("summary", "editor-tree-dir", node.name || "workspace");
      details.appendChild(summary);
      const children = el("div", "editor-tree-children");
      details.appendChild(children);
      (node.children || []).forEach((child) => render(child, children));
      container.appendChild(details);
    };
    (tree.children || []).forEach((child) => render(child, treeHost));
    state.treeLoaded = true;
  }

  /* ---------------- visibility + external hooks ---------------- */
  function showWorkspace() {
    workspace.hidden = false;
    workspace.closest(".agent-output-panel")?.classList.add("editor-mode");
    if (!state.treeLoaded) renderTree();
  }

  function toggle() {
    if (workspace.hidden) showWorkspace();
    else {
      workspace.hidden = true;
      workspace.closest(".agent-output-panel")?.classList.remove("editor-mode");
    }
  }

  toggleBtn.addEventListener("click", toggle);

  /** Called from app.js on file.changed events. */
  window.jevoraEditorFileChanged = function (path) {
    const tab = state.tabs.get(path);
    if (!tab) return;
    if (tab.dirty) { tab.stale = true; renderTabs(); return; }
    api.get(`/api/workspace/file?path=${encodeURIComponent(path)}`)
      .then((file) => {
        if (file.binary) return;
        tab.original = file.content;
        if (tab.model) { tab.model.setValue(file.content); tab.dirty = false; }
        if (tab.textarea) { tab.textarea.value = file.content; tab.dirty = false; }
        renderTabs();
      })
      .catch(() => { /* file may be gone */ });
  };

  /** Overrides panels.js's modal: files open as editor tabs. */
  window.jevoraOpenEditor = function (path, workspaceId) { openTab(path, workspaceId); };

  function toast(msg) {
    const container = document.querySelector(".toast-container");
    if (!container) return;
    const n = el("div", "toast", msg);
    container.appendChild(n);
    setTimeout(() => n.remove(), 3500);
  }
  }

  if (document.readyState !== "loading") main();
  else document.addEventListener("DOMContentLoaded", main);
})();
