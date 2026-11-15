/* ==========================================================================
   Jevora IDE — Theme engine + picker
   Works with the data-theme system in styles.css. The theme is applied
   before first paint by the inline bootstrap in index.html; this file owns
   every runtime change:
     - window.JevoraThemes.apply(id)  — single entry point used by the
       sidebar picker AND the Settings → Appearance tab
     - "jevora:themechange" event     — lets both UIs re-sync their state
   ========================================================================== */
(function () {
  "use strict";

  var STORAGE_KEY = "jevora.theme";

  var THEMES = [
    {
      id: "cursor-dark", label: "Cursor Dark", hint: "VS Code-style", meta: "#1E1E1E",
      palette: { bg: "#1E1E1E", side: "#181818", accent: "#3794FF", text: "#D4D4D4" }
    },
    {
      id: "jetbrains-dark", label: "JetBrains Dark", hint: "New UI", meta: "#1E1F22",
      palette: { bg: "#1E1F22", side: "#2B2D30", accent: "#3574F0", text: "#DFE1E5" }
    },
    {
      id: "zed-dark", label: "Graphite Dark", hint: "Zed-style", meta: "#111113",
      palette: { bg: "#111113", side: "#18181B", accent: "#2FBFAD", text: "#E4E4E7" }
    },
    {
      id: "github-light", label: "GitHub Light", hint: "Industry light", meta: "#FFFFFF",
      palette: { bg: "#FFFFFF", side: "#F6F8FA", accent: "#0969DA", text: "#1F2328" }
    }
  ];

  function isKnown(themeId) {
    return THEMES.some(function (t) { return t.id === themeId; });
  }

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    return isKnown(t) ? t : "cursor-dark";
  }

  function persist(themeId) {
    try { localStorage.setItem(STORAGE_KEY, themeId); } catch (_) { /* private mode */ }
  }

  function syncMeta(themeId) {
    var theme = THEMES.find(function (t) { return t.id === themeId; });
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta && theme) meta.setAttribute("content", theme.meta);
  }

  /* Single entry point for changing the theme at runtime. Returns the
     applied theme id (unknown ids are ignored and current is kept). */
  function applyTheme(themeId) {
    if (!isKnown(themeId)) return currentTheme();
    document.documentElement.setAttribute("data-theme", themeId);
    persist(themeId);
    syncMeta(themeId);
    try {
      document.dispatchEvent(new CustomEvent("jevora:themechange", { detail: { theme: themeId } }));
    } catch (_) { /* old browsers */ }
    return themeId;
  }

  // Shared API — used by the sidebar picker below and Settings → Appearance.
  window.JevoraThemes = { list: THEMES, current: currentTheme, apply: applyTheme };

  /* ---------------- Sidebar picker menu ---------------- */

  function syncMenu() {
    var active = currentTheme();
    var items = document.querySelectorAll(".theme-menu-item");
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle("active", items[i].getAttribute("data-theme-value") === active);
    }
    var btn = document.getElementById("themePickerBtn");
    if (btn) btn.setAttribute("aria-expanded", "false");
  }

  function closeMenu() {
    var menu = document.getElementById("themeMenu");
    if (menu) menu.hidden = true;
    syncMenu();
  }

  function openMenu() {
    var menu = document.getElementById("themeMenu");
    var btn = document.getElementById("themePickerBtn");
    if (!menu || !btn) return;
    syncMenu();
    // The sidebar clips absolutely-positioned children, so anchor the menu
    // with position: fixed just below the picker button.
    var rect = btn.getBoundingClientRect();
    menu.style.left = Math.max(8, rect.left - 8) + "px";
    menu.style.top = rect.bottom + 8 + "px";
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }

  function wire() {
    var btn = document.getElementById("themePickerBtn");
    var menu = document.getElementById("themeMenu");
    if (!btn || !menu) return;

    syncMenu();

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });

    menu.addEventListener("click", function (e) {
      var item = e.target.closest ? e.target.closest(".theme-menu-item") : null;
      if (!item) return;
      var themeId = item.getAttribute("data-theme-value");
      if (!themeId) return;
      applyTheme(themeId);
      closeMenu();
    });

    // Keep the menu in sync when the theme changes elsewhere (Settings tab).
    document.addEventListener("jevora:themechange", function () {
      if (!menu.hidden) syncMenu();
    });

    document.addEventListener("click", function (e) {
      if (!menu.hidden && !document.getElementById("themePicker").contains(e.target)) closeMenu();
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !menu.hidden) closeMenu();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
