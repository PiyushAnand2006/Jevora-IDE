# Jevora IDE — Desktop (Electron) Shell

The desktop app wraps the FastAPI backend + static frontend in an Electron
shell, following the same architecture as the **Emberling** project: single
instance, preload bridge with `contextIsolation`, tray, and auto-update via
`electron-updater` ↔ GitHub releases. The one addition Emberling doesn't
have: a managed Python backend child process.

## Architecture

```
electron/
  main.js      app lifecycle: single instance, window, tray, updater, IPC
  backend.js   Python backend process manager (spawn, port pick, health wait,
               log capture, graceful shutdown)
  preload.js   window.jevora bridge (backend status + updater controls)
packaging/
  backend_main.py  uvicorn entry shared by dev and the frozen build
  build_backend.py PyInstaller onedir build -> dist-backend/jevora-backend/
  make_icon.py     generates build/icon.ico + build/icon.png
frontend/shell.js update banner + backend pill (active only under Electron)
```

How a launch works:

1. Electron main starts, acquires the single-instance lock.
2. `BackendManager` picks a free port (8000–8010), spawns the backend
   (dev: repo `.venv` python; packaged: `resources/jevora-resources/backend/jevora-backend.exe`),
   and polls `http://127.0.0.1:<port>/api/system/health` until ready.
3. The window loads the backend URL — the exact same UI/API as the web mode,
   including websockets and cookies.
4. On quit the backend receives `SIGBREAK` (Windows) / `SIGTERM`, runs its
   lifespan shutdown hooks (browser-control sessions, sandboxes), then exits;
   a `SIGKILL` fallback fires after 8 s.

Security posture of the shell: the only two executables the manager can spawn
are fixed, existence-checked paths; every argv is a literal array (variability
travels through env); the only URL ever requested is the child's health
endpoint on `127.0.0.1:<port>`.

Packaged data locations (all under `%APPDATA%/Jevora IDE/`): `data/`
(SQLite DB + vault), `workspace/`, `logs/backend-*.log`, and an optional
user-editable `.env` honored by the backend.

## Development

```bash
npm install          # electron, electron-builder, electron-updater
npm run dev          # = electron . — spawns the .venv backend automatically
```

`npm run smoke` runs the headless stack check (`--smoke-test`): boots the
backend, reports state/port, exits 0 when healthy (report also written to the
profile dir as `smoke-report.json`).

## Build & release

```bash
npm run icon         # regenerate build/icon.ico|png
npm run backend:build  # PyInstaller onedir -> dist-backend/jevora-backend/
npm run dist         # backend build + electron-builder (NSIS + portable)
```

Artifacts land in `release/`:

- `Jevora-IDE-Setup-<version>.exe` — NSIS installer
- `Jevora-IDE-Portable-<version>.exe` — portable build
- `latest.yml` — the update manifest electron-updater fetches

### Auto-update behavior (same policy as Emberling)

- Packaged builds check GitHub releases (`PiyushAnand2006/Jevora-IDE`)
  ~6 s after launch and then every 6 h.
- NSIS installs: updates download in the background and a banner offers
  **Restart to update**; the update also applies silently on quit.
- Portable builds can't self-install — the banner links to the releases page.
- Checks are quiet when offline; failures never spam the UI.

To ship a release: bump `version` in `package.json`, run `npm run dist`,
then create a GitHub release for the matching tag and upload
`Jevora-IDE-Setup-<version>.exe`, `Jevora-IDE-Portable-<version>.exe`,
and `latest.yml` from `release/`. Installed apps pick it up automatically.
