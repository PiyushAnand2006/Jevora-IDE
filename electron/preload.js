// Jevora IDE — preload bridge (contextIsolation, no nodeIntegration).
// Mirrors Emberling's pattern: a narrow `window.<app>` API; every channel is
// an explicit allowlisted name. The web build simply sees `window.jevora`
// as undefined, so all renderer code must optional-chain it.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("jevora", {
  platform: process.platform,
  versions: { electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node },

  backend: {
    status: () => ipcRenderer.invoke("backend:status"),
    onStatus: (cb) => {
      const listener = (_e, data) => cb(data);
      ipcRenderer.on("backend:status", listener);
      return () => ipcRenderer.removeListener("backend:status", listener);
    },
  },

  updater: {
    check: () => ipcRenderer.invoke("updater:check"),
    install: () => ipcRenderer.send("updater:install"),
    openReleases: () => ipcRenderer.invoke("updater:openReleases"),
    onEvent: (cb) => {
      const listener = (_e, payload) => cb(payload);
      ipcRenderer.on("updater:event", listener);
      return () => ipcRenderer.removeListener("updater:event", listener);
    },
  },
});
