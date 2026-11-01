// Jevora IDE — Electron main process.
// Structure mirrors Emberling: single-instance lock, preload bridge with
// contextIsolation, tray, auto-update through electron-updater ↔ GitHub
// releases — plus the one thing EmberLink doesn't have: a managed Python
// backend child (see electron/backend.js).

const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs");

const { BackendManager, isDev } = require("./backend");

// dev uses a deterministic in-repo profile: same data as the web server, and
// no dependence on the OS profile dir's state (crashpad/GPU caches there have
// caused silent aborts on this machine). Packaged builds keep the default.
if (isDev()) {
  app.setPath("userData", path.join(__dirname, "..", ".jevora-data", "electron"));
}

// Chromium's sandbox broker aborts the whole process on some Windows setups
// (security software / policy) with exit 3 before any app code can log —
// `--no-sandbox` restores it (verified on this machine). The npm dev/start
// scripts pass the switch; this env-var escape hatch covers packaged runs.
// Trade-off: renderer process-isolation hardening is off when enabled.
if (process.env.JEVORA_DISABLE_SANDBOX === "1") {
  app.commandLine.appendSwitch("no-sandbox");
}

// optional isolated profile (also scopes the single-instance lock):
// Jevora.exe --user-data-dir=C:\path
const uddArg = process.argv.find((a) => a.startsWith("--user-data-dir="));
if (uddArg) app.setPath("userData", uddArg.slice("--user-data-dir=".length));

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  bootstrap();
}

let mainWindow = null;
let tray = null;
let backend = null; // BackendManager, set in bootstrap
let updateApi = null; // set by setupAutoUpdate when packaged
let quitting = false;

/* ---------------- Auto-update (electron-updater ↔ GitHub releases) ----------------
 * Packaged NSIS installs: check GitHub for the latest release (latest.yml),
 * download in the background, and surface a "Restart to update" banner —
 * a pending update also applies silently on quit if the banner is dismissed.
 * Portable builds can't self-install; they get a "get it from GitHub" button.
 * Same policy as Emberling: download automatically, install on quit. */
function setupAutoUpdate() {
  if (!app.isPackaged) return null;
  const portable = Boolean(process.env.PORTABLE_EXECUTABLE_DIR);
  let updater;
  try {
    ({ autoUpdater: updater } = require("electron-updater"));
  } catch (err) {
    console.warn("auto-update unavailable:", err?.message);
    return null;
  }
  const push = (payload) => {
    if (!mainWindow?.isDestroyed()) mainWindow.webContents.send("updater:event", payload);
  };
  updater.autoDownload = !portable;
  updater.autoInstallOnAppQuit = true;
  updater.on("update-available", (info) => push({ state: portable ? "available" : "downloading", version: info.version, portable }));
  updater.on("download-progress", (p) => push({ state: "downloading", percent: Math.round(p.percent || 0), version: p.version }));
  updater.on("update-downloaded", (info) => push({ state: "ready", version: info.version }));
  updater.on("error", (err) => {
    console.warn("update check failed:", err?.message);
    push({ state: "error", message: String(err?.message || err).slice(0, 200) });
  });
  const check = () => updater.checkForUpdates();
  // shortly after launch, then every 6 hours; offline failures stay quiet
  setTimeout(() => check().catch(() => {}), 6000);
  setInterval(() => check().catch(() => {}), 6 * 60 * 60 * 1000);
  return { check, install: () => updater.quitAndInstall(true, true), portable };
}

function appIcon() {
  const candidates = [
    path.join(process.resourcesPath || "", "icon.png"),
    path.join(__dirname, "..", "build", "icon.png"),
  ];
  for (const p of candidates) {
    if (p && fs.existsSync(p)) return nativeImage.createFromPath(p);
  }
  return null;
}

function createMainWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 940,
    minHeight: 600,
    backgroundColor: "#0f1115",
    show: false,
    icon: appIcon() ?? undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: true,
    },
  });
  mainWindow.on("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  // external links open in the user's browser, never inside the app window
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//i.test(target)) shell.openExternal(target);
    return { action: "deny" };
  });
  mainWindow.loadURL(url);
}

function createTray() {
  const icon = appIcon()?.resize({ width: 32, height: 32 });
  if (!icon) return null;
  tray = new Tray(icon);
  const rebuild = () => {
    const template = [
      { label: `Jevora IDE ${app.getVersion()} — backend ${backend?.state ?? "stopped"}`, enabled: false },
      { type: "separator" },
      { label: "Open Jevora IDE", click: () => focusMain() },
      { label: "Check for updates", click: () => updateApi?.check?.().catch(() => {}) },
      { type: "separator" },
      { label: "Quit Jevora IDE", click: () => app.quit() },
    ];
    tray.setContextMenu(Menu.buildFromTemplate(template));
    tray.setToolTip("Jevora IDE");
  };
  tray.on("click", () => focusMain());
  rebuild();
  backend?.on("status", rebuild);
  return rebuild;
}

function focusMain() {
  if (!mainWindow) {
    if (backend?.baseUrl) createMainWindow(backend.baseUrl);
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function pushBackendStatus(status) {
  if (!mainWindow?.isDestroyed()) mainWindow?.webContents.send("backend:status", status);
}

function bootstrap() {
  app.on("second-instance", () => focusMain());

  app.whenReady().then(async () => {
    backend = new BackendManager({ userDataDir: app.getPath("userData") });
    backend.on("status", (status) => {
      pushBackendStatus(status);
      if (tray) tray.setToolTip(`Jevora IDE — backend ${status.state}${status.port ? " :" + status.port : ""}`);
    });

    // the window loads once the backend is healthy; a splash-free wait is
    // fine because uvicorn boots in a couple of seconds
    let url;
    try {
      url = await backend.start();
    } catch (err) {
      console.error(err?.message);
    }
    if (!url) {
      const { dialog } = require("electron");
      dialog.showErrorBox("Jevora IDE — backend failed to start", (backend.logs || "").slice(-1500) || "unknown error");
      app.quit();
      return;
    }

    createTray();
    createMainWindow(url);
    updateApi = setupAutoUpdate();

    ipcMain.handle("updater:check", async () => {
      const current = app.getVersion();
      const portable = Boolean(process.env.PORTABLE_EXECUTABLE_DIR);
      if (!updateApi) {
        return { ok: true, current, state: !app.isPackaged ? "dev" : "unavailable", portable };
      }
      try {
        const result = await updateApi.check();
        const latest = String(result?.updateInfo?.version || "");
        return { ok: true, current, latest, updateAvailable: Boolean(latest) && latest !== current, portable };
      } catch (err) {
        return { ok: false, current, portable, error: String(err?.message || err).slice(0, 200) };
      }
    });
    ipcMain.on("updater:install", () => updateApi?.install());
    ipcMain.handle("updater:openReleases", () => shell.openExternal("https://github.com/PiyushAnand2006/Jevora-IDE/releases/latest"));
    ipcMain.handle("backend:status", () => ({ state: backend.state, port: backend.port, url: backend.baseUrl, logs: backend.logs }));
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", (event) => {
    if (quitting) return;
    quitting = true;
    // stop the backend child before Electron tears down the loop
    event.preventDefault();
    backend?.stop().finally(() => app.exit(0));
  });
}

// smoke-test hook: `electron . --smoke-test` boots the stack, reports, exits
if (process.argv.includes("--smoke-test")) {
  app.whenReady().then(async () => {
    const deadline = Date.now() + 150_000;
    while (backend === null && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
    // wait until the backend reaches a terminal lifecycle state (the manager
    // starts in "stopped" and moves through "starting" to "ready"/"error")
    while (backend && ["stopped", "starting"].includes(backend.state) && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
    }
    const report = {
      state: backend?.state,
      port: backend?.port,
      url: backend?.baseUrl,
      packaged: app.isPackaged,
      version: app.getVersion(),
      updateApi: Boolean(updateApi),
      logTail: (backend?.logs || "").slice(-800),
    };
    try {
      // app.exit() can truncate pending stdout; persist the report to a fixed
      // location inside the profile dir (never an argv-supplied path)
      require("node:fs").writeFileSync(path.join(app.getPath("userData"), "smoke-report.json"),
        JSON.stringify(report, null, 2));
    } catch { /* best effort */ }
    console.log("SMOKE:" + JSON.stringify(report));
    app.exit(backend?.state === "ready" ? 0 : 1);
  });
}
