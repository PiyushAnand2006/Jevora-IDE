/**
 * Jevora IDE — Electron shell integration (optional at runtime).
 *
 * Loaded last from index.html. When the app runs inside the Jevora desktop
 * shell, `window.jevora` is injected by the preload bridge; in a plain
 * browser it stays undefined and this module does nothing. Mirrors the
 * Emberling updater UX: a small banner for update state + restart-to-update,
 * and a quiet backend-status indicator.
 */
(function () {
  "use strict";

  var shell = window.jevora;
  if (!shell) return; // plain browser — nothing to integrate

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ---------------- update banner ---------------- */

  var banner = null;

  function ensureBanner() {
    if (banner) return banner;
    banner = el("div", "jevora-update-banner");
    banner.setAttribute("role", "status");
    var label = el("span", "jevora-update-label");
    var actions = el("span", "jevora-update-actions");
    var install = el("button", "jevora-update-btn");
    install.textContent = "Restart to update";
    install.addEventListener("click", function () { shell.updater.install(); });
    var releases = el("button", "jevora-update-btn jevora-update-btn-secondary");
    releases.textContent = "Get it from GitHub";
    releases.addEventListener("click", function () { shell.updater.openReleases(); });
    actions.appendChild(install);
    actions.appendChild(releases);
    banner.appendChild(label);
    banner.appendChild(actions);
    document.body.appendChild(banner);
    return banner;
  }

  function setBanner(text, mode) {
    var node = ensureBanner();
    node.querySelector(".jevora-update-label").textContent = text;
    node.dataset.mode = mode || "info";
    var installBtn = node.querySelector(".jevora-update-btn");
    var ghBtn = node.querySelector(".jevora-update-btn-secondary");
    if (mode === "ready") {
      installBtn.style.display = "";
      ghBtn.style.display = "none";
    } else if (mode === "portable") {
      installBtn.style.display = "none";
      ghBtn.style.display = "";
    } else {
      installBtn.style.display = "none";
      ghBtn.style.display = "none";
    }
    node.classList.add("visible");
  }

  function hideBanner() {
    if (banner) banner.classList.remove("visible");
  }

  shell.updater.onEvent(function (payload) {
    if (!payload) return;
    if (payload.state === "downloading") {
      setBanner("Downloading update" + (payload.version ? " v" + payload.version : "") +
        (payload.percent != null ? " — " + payload.percent + "%" : ""), "info");
    } else if (payload.state === "available") {
      setBanner("Update v" + payload.version + " available — this portable build can't self-install", "portable");
    } else if (payload.state === "ready") {
      setBanner("Update v" + payload.version + " ready", "ready");
    } else if (payload.state === "error") {
      // stay quiet: offline machines must not see error spam
      hideBanner();
    }
  });

  // one explicit manual check, for parity with Emberling's settings entry
  shell.updater.check().then(function (result) {
    if (result && result.ok && result.updateAvailable && result.portable) {
      setBanner("Update v" + result.latest + " available", "portable");
    }
  }).catch(function () { /* offline: quiet */ });

  /* ---------------- backend status pill ---------------- */

  var pill = null;
  function ensurePill() {
    if (pill) return pill;
    pill = el("div", "jevora-backend-pill");
    pill.title = "Jevora backend status (Electron shell)";
    document.body.appendChild(pill);
    return pill;
  }

  function setPill(state, port) {
    var node = ensurePill();
    node.dataset.state = state;
    node.textContent = state === "ready" ? ("api :" + port) : state;
  }

  shell.backend.onStatus(function (status) {
    if (!status) return;
    if (status.state === "ready" || status.state === "error") {
      setPill(status.state, status.port);
      if (status.state === "error") setTimeout(hideBannerWrapper, 100);
    } else {
      setPill(status.state, status.port);
    }
  });

  function hideBannerWrapper() { /* reserved for error surfacing */ }

  /* ---------------- styles (injected once) ---------------- */

  var css = document.createElement("style");
  css.textContent =
    ".jevora-update-banner{position:fixed;top:12px;right:12px;z-index:10000;display:none;" +
    "align-items:center;gap:10px;padding:10px 14px;border-radius:10px;" +
    "background:#312e81;color:#eef2ff;font:13px/1.4 system-ui,sans-serif;" +
    "box-shadow:0 8px 24px rgba(0,0,0,.35)}" +
    ".jevora-update-banner.visible{display:flex}" +
    ".jevora-update-actions{display:flex;gap:8px}" +
    ".jevora-update-btn{border:0;border-radius:8px;padding:6px 12px;cursor:pointer;" +
    "background:#6366f1;color:#fff;font:600 12px system-ui,sans-serif}" +
    ".jevora-update-btn:hover{background:#818cf8}" +
    ".jevora-update-btn-secondary{background:#4c1d95}" +
    ".jevora-backend-pill{position:fixed;bottom:10px;right:12px;z-index:10000;" +
    "padding:3px 10px;border-radius:999px;background:rgba(49,46,129,.85);color:#c7d2fe;" +
    "font:600 11px system-ui,sans-serif}" +
    ".jevora-backend-pill[data-state='ready']{background:rgba(6,78,59,.85);color:#a7f3d0}" +
    ".jevora-backend-pill[data-state='error']{background:rgba(127,29,29,.9);color:#fecaca}";
  document.head.appendChild(css);
})();
