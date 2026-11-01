// Jevora IDE — backend process manager (Electron main process).
//
// EmberLink is a pure-JS app; Jevora additionally ships a Python FastAPI
// backend. This module owns that child process:
//   * dev      -> spawn the repo's .venv python with packaging/backend_main.py
//   * packaged -> spawn the PyInstaller onedir server under resources/
// It picks a free port, waits for /api/system/health, captures logs into the
// user-data dir, and shuts the server down on quit (uvicorn handles SIGBREAK
// gracefully on Windows; SIGKILL is the fallback — no external binaries are
// ever spawned by this module).
//
// Security posture (enforced below, not just documented):
//   * The ONLY two executables this module can launch are the fixed,
//     existence-checked backend paths — no user data ever reaches argv.
//     Every argv is a literal array; all variability (host/port/data dirs)
//     travels through environment variables instead.
//   * The only URL ever requested is this child's health endpoint on
//     127.0.0.1:<port>, plain http — localhost-only by construction, so no
//     SSRF surface exists here.

const { spawn } = require("node:child_process");
const http = require("node:http");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { createServer } = require("node:net");

const LOG_RING_MAX = 500; // lines kept in memory for the renderer/logs dialog

function isDev() {
  // keep separate so tests can stub process.defaultApp
  return Boolean(process.defaultApp || process.env.JEVORA_ELECTRON_DEV);
}

function repoRoot() {
  // electron/backend.js -> repo root is one level up
  return path.join(__dirname, "..");
}

/** Fixed candidate 1: the repo's virtualenv interpreter (dev mode only). */
function venvPython() {
  const win = path.join(repoRoot(), ".venv", "Scripts", "python.exe");
  const nix = path.join(repoRoot(), ".venv", "bin", "python");
  return fs.existsSync(win) ? win : nix;
}

/** Fixed candidate 2: the PyInstaller onedir server bundled as a resource. */
function packagedServerExe() {
  const base = process.resourcesPath || path.join(repoRoot(), "dist-backend");
  const exe = process.platform === "win32" ? "jevora-backend.exe" : "jevora-backend";
  return path.join(base, "jevora-backend", exe);
}

/** First free port in 8000..8010, or the preferred port if all are busy. */
function pickPort(preferred = 8000) {
  return new Promise((resolve) => {
    const tryPort = (p) => {
      const srv = createServer();
      srv.once("error", () => (p < 8010 ? tryPort(p + 1) : resolve(preferred)));
      srv.once("listening", () => srv.close(() => resolve(p)));
      srv.listen(p, "127.0.0.1");
    };
    tryPort(preferred);
  });
}

class BackendManager extends EventEmitter {
  constructor({ userDataDir }) {
    super();
    this.userDataDir = userDataDir;
    this.proc = null;
    this.port = null;
    this.state = "stopped"; // stopped | starting | ready | error
    this.logRing = [];
    this.logFile = null;
    this.stopping = false;
  }

  get baseUrl() {
    return this.port ? `http://127.0.0.1:${this.port}` : null;
  }
  get logs() {
    return this.logRing.slice(-LOG_RING_MAX).join("");
  }

  _setState(state, extra = {}) {
    this.state = state;
    this.emit("status", { state, port: this.port, ...extra });
  }

  _log(line) {
    const text = String(line || "");
    this.logRing.push(text);
    if (this.logRing.length > LOG_RING_MAX * 2) this.logRing.splice(0, this.logRing.length - LOG_RING_MAX);
    if (this.logFile) fsp.appendFile(this.logFile, text).catch(() => {});
  }

  /** The fixed launch plan: two literal argv arrays, one of two executables. */
  _launchPlan(port, userDataDir) {
    const dataDir = path.join(userDataDir, "data");
    const sharedEnv = {
      JEVORA_HOST: "127.0.0.1",
      JEVORA_PORT: String(port),
      DATA_DIR: dataDir,
      WORKSPACE_ROOT: path.join(userDataDir, "workspace"),
      // durable local store inside the profile dir unless the user opted into
      // something else (Postgres) via their own environment
      DATABASE_URL: process.env.DATABASE_URL ?? `sqlite+aiosqlite:///${dataDir.replace(/\\/g, "/")}/jevora.db`,
      BROWSER_CONTROL_ENABLED: process.env.BROWSER_CONTROL_ENABLED ?? "true",
      BROWSER_DCOM_ENABLED: process.env.BROWSER_DCOM_ENABLED ?? "true",
      BROWSER_DESKTOP_CAPTURE_ENABLED: process.env.BROWSER_DESKTOP_CAPTURE_ENABLED ?? "true",
    };
    if (isDev()) {
      // dev shares the repo's local data so the desktop app and a separately
      // started web server see the same users/runs (the packaged app uses the
      // OS profile dir instead)
      const dataDir = path.join(repoRoot(), ".jevora-data");
      return {
        cmd: venvPython(),
        // literal argv; variability travels only through env
        args: ["packaging/backend_main.py"],
        cwd: repoRoot(),
        env: {
          ...process.env,
          ...sharedEnv,
          DATA_DIR: dataDir,
          WORKSPACE_ROOT: path.join(repoRoot(), "workspace"),
          DATABASE_URL: process.env.DATABASE_URL ?? `sqlite+aiosqlite:///${dataDir.replace(/\\/g, "/")}/jevora.db`,
          PYTHONPATH: repoRoot(),
          FRONTEND_DIR: path.join(repoRoot(), "frontend"),
        },
      };
    }
    return {
      cmd: packagedServerExe(),
      args: [],
      cwd: userDataDir, // a user-editable .env in userData is honored
      env: {
        ...process.env,
        ...sharedEnv,
        FRONTEND_DIR: path.join(process.resourcesPath || "", "frontend"),
      },
    };
  }

  async start() {
    if (this.proc) return this.baseUrl;
    const port = await pickPort(8000);
    this.port = port;

    const logsDir = path.join(this.userDataDir, "logs");
    await fsp.mkdir(logsDir, { recursive: true });
    this.logFile = path.join(logsDir, `backend-${new Date().toISOString().slice(0, 10)}.log`);

    const plan = this._launchPlan(port, this.userDataDir);
    if (!fs.existsSync(plan.cmd)) {
      const hint = isDev()
        ? "dev venv not found — run `python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt`"
        : "bundled backend missing (broken install)";
      this._setState("error", { error: hint });
      throw new Error(hint + " (" + plan.cmd + ")");
    }

    this._setState("starting", { cmd: plan.cmd });
    this.proc = spawn(plan.cmd, plan.args, {
      cwd: plan.cwd,
      env: plan.env,
      windowsHide: true,
      shell: false,
    });
    this.proc.stdout.on("data", (d) => this._log(d));
    this.proc.stderr.on("data", (d) => this._log(d));
    this.proc.on("exit", (code) => {
      this._log(`[backend exited code=${code}]`);
      this.proc = null;
      if (!this.stopping) this._setState("error", { error: `backend exited unexpectedly (code=${code})` });
      else this._setState("stopped");
    });

    const ok = await this._waitHealthy(120_000);
    if (ok) this._setState("ready");
    else if (this.state !== "error") this._setState("error", { error: "backend did not become healthy in time" });
    return ok ? this.baseUrl : null;
  }

  /** Poll our own child's health endpoint on 127.0.0.1:<port> until ready. */
  _waitHealthy(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    return new Promise((resolve) => {
      const attempt = () => {
        if (this.stopping || !this.proc || Date.now() > deadline) return resolve(false);
        const url = `http://127.0.0.1:${this.port}/api/system/health`;
        const req = http.get(url, { timeout: 2000 }, (res) => {
          res.resume();
          if (res.statusCode === 200) resolve(true);
          else retry();
        });
        req.on("error", retry);
        req.on("timeout", () => {
          req.destroy();
          retry();
        });
      };
      const retry = () => {
        setTimeout(attempt, 700);
      };
      attempt();
    });
  }

  async stop() {
    this.stopping = true;
    const proc = this.proc;
    this.proc = null;
    if (!proc) {
      this.stopping = false;
      return;
    }
    await new Promise((resolve) => {
      // hard-stop fallback: uvicorn normally exits long before this fires,
      // having run its lifespan shutdown hooks (browser sessions, sandboxes)
      const killer = setTimeout(() => {
        try { proc.kill("SIGKILL"); } catch { /* already gone */ }
        resolve();
      }, 8000);
      killer.unref();
      proc.once("exit", () => {
        clearTimeout(killer);
        resolve();
      });
      const signal = process.platform === "win32" ? "SIGBREAK" : "SIGTERM";
      try { proc.kill(signal); } catch { clearTimeout(killer); resolve(); }
    });
    this._setState("stopped");
    this.stopping = false;
  }
}

module.exports = { BackendManager, pickPort, isDev };
