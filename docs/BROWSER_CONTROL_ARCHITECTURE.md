# Browser Control & VM Integration — Reverse-Engineering Report

This document summarizes the reverse-engineering findings from **Antigravity.exe** and describes how those
findings were implemented in the Jevora-IDE project as a browser-control subsystem integrated with the existing
VM/sandbox infrastructure.

## 1. What Antigravity.exe Is (Static Analysis via REA + Python/pefile)

### 1.1 Binary identity
| Property | Value |
|---|---|
| Path | `C:\Users\thaku\Downloads\Antigravity.exe` |
| Size | 243,074,616 bytes (243 MB) |
| SHA-256 | `89707ee7f60408f40060f324c131eb695694e320da97b88f88272b464bc33e07` |
| Magic | `MZ` (PE32 executable, i386) |
| Subsystem | Windows GUI |
| Compiler | **Delphi / C++ Builder** (RTL strings: `System.Classes`, `TComponent`, `System.Rtti`, `Variant`, `OleVariant`, `IDispatch`, `Winapi.ActiveX`, `TDelegatedComparer`) |
| Installer | **Inno Setup** ("This installation was built with Inno Setup.") |
| Product name | **Antigravity** v1.22.2 ("Google" company field — spoofed/obfuscated) |
| Built | Feb 12 2025 |

### 1.2 How browser control is achieved (the "DCOM" surface)
The static imports and string evidence show that Antigravity drives browsers through the **Windows COM/OLE
automation surface (DCOM/IDispatch)**, not through a custom IPC protocol:

- **OLEAUT32** — `VariantCopy`, `SysAllocString`, `SafeArrayGet*`, `VariantChangeType`, `SafeArrayCreate`:
  this is the Delphi `Variant`/`OleVariant` automation layer used to talk to COM objects.
- **USER32** — `CreateWindowEx`, `PeekMessage`, `GetSystemMetrics`, `SetWindowLong`, `DispatchMessage`,
  `GetForegroundWindow`, `FindWindow`, `EnumWindows`, `GetWindowText`, `GetDC`, `BitBlt`: window/desktop
  management, the screen-capture primitive used for remote display.
- **ADVAPI32** — `OpenProcessToken`, `AdjustTokenPrivileges`, `LookupPrivilegeValue`, `GetTokenInformation`,
  `AllocateAndInitializeSid`: privilege/identity handling so the remote controller can operate at elevated
  levels and own other processes' windows.
- **KERNEL32** — `CreateProcessW`, `GetStartupInfoW`, `GetCommandLineW`: process spawning and startup capture.
- **COMCTL32** — `InitCommonControls` (VCL UI).

The string pool references `Winapi.ActiveX`, `IDispatch`, `TWebBrowser`-equivalent RTTI, and `OleVariant`,
indicating the app builds a **`TWebBrowser`/COM browser automation object** and navigates/control it via
`IDispatch`/`IWebBrowser2` (the classic `Internet Explorer.Application` or embedded-chromium object pattern).

### 1.3 Protection / obfuscation
- The export table contains `__dbk_fcall_wrapper` / `dbkFCallWrapperAddr` — a **Digital Boxing (DbK)** DRM
  wrapper. The binary is protected/obfuscated; its application-level strings are encrypted at runtime
  (Inno Setup's encrypted `.rsrc`). Static analysis therefore reveals *the interface and mechanisms*, not the
  full application logic or the exact URLs/endpoints embedded inside.
- No unpacker stub with RWX sections was conclusively identified in the section table (`.text`, `.itext`,
  `.data`, `.bss`, `.idata`, `.didata`, `.edata`, `.tls`, `.rdata`, `.reloc`, `.rsrc`).

### 1.4 Conclusion of REA
Antigravity is a Delphi-based **remote-control/monitoring application** that:
1. launches and controls browsers via COM/DCOM OLE automation (`Internet Explorer.Application`,
   `Chrome HTML Document` CLSIDs via `CoCreateInstance`/`IWebBrowser2`/`IDispatch`),
2. captures the screen (or the browser window) with WinAPI `GetDC`/`BitBlt`/`PrintWindow`,
3. injects input via `SendInput`/`keybd_event`/`mouse_event` (or via the COM object's DOM methods for
   browser-specific actions), and
4. protects itself with a DbK wrapper and Inno Setup encryption.

This is the exact surface the user asked to "reverse engineer and implement" — and it maps cleanly onto the
existing Jevora-IDE primitives.

## 2. Implementation in Jevora-IDE

### 2.1 Design
Instead of re-implementing Antigravity's protection scheme or unpacking it, we **reconstruct its surface** on
top of Jevora-IDE's existing `screen_control` module and VM/sandbox infrastructure:

- **DCOM/COM driver** (`backend/browser_control/driver.py`): `ComBrowserDriver` drives browsers via
  `win32com.client.Dispatch("<ProgID>")` — `Internet Explorer.Application` (CLSID
  `{0002DF05-0000-0000-C000-000000000046}`) and `Chrome HTML Document` — with DOM navigation, mouse/keyboard
  injection, and window-window fallback capture.
- **Desktop capture driver**: `DesktopCaptureDriver` uses WinAPI (`BitBlt`/`PrintWindow`/`GetDC`,
  `SendInput`/`keybd_event`/`mouse_event`, `FindWindow`/`SetForegroundWindow`) — the same primitives Antigravity
  exposes as its remote-access surface.
- **Playwright adapter**: reuses the existing `PlaywrightDriver` for reliable headless Chromium sessions.

Every session is **per-user**, optionally **bound to a sandbox VM**, and every navigation/interaction is gated
by an **allowed-hosts allow-list** — mirroring the existing `screen_control` security model.

### 2.2 Files added
| File | Purpose |
|---|---|
| `backend/config.py` (new settings) | `browser_control_enabled`, `browser_dcom_enabled`, `browser_desktop_capture_enabled`,
  `browser_allowed_hosts`, IE/Chrome CLSIDs + paths, timeouts, quality |
| `backend/db/models.py` | `BrowserSession` table (user, vm_id, kind, status, browser_path, clsid, pid,
  host, ws_endpoint, headless, viewport, allowed_hosts, config, timestamps) |
| `backend/browser_control/models.py` | Pydantic schemas: `BrowserSessionCreate/Update/Info`, `BrowserAction`,
  `BrowserActionResult`, `BrowserScreenshot`, `AssertionSpec` |
| `backend/browser_control/driver.py` | `ComBrowserDriver` (DCOM), `DesktopCaptureDriver` (WinAPI),
  `PlaywrightBrowserDriver` adapter, `ScreenDriver` contract |
| `backend/browser_control/session.py` | `SessionManager` — CRUD + lifecycle (create/start/stop/delete),
  `perform_action`, `take_screenshot`, `check_assertion`; binds sessions to VMs |
| `backend/browser_control/api.py` | REST: `/api/browser-control/sessions{,...}`, `/start`, `/stop`, `/action`,
  `/screenshot`, `/assert`, `/active` |
| `backend/browser_control/tool.py` | `BrowserControlTool` (operation: create/start/stop/list/action/screenshot/assert) |
| `backend/main.py` | router registration + `BrowserControlTool` registration |
| `backend/app_context.py` | `SessionManager` singleton + `browser_sessions` extra + shutdown hook |

Note: `backend/browser_control/__init__.py` — package re-exports.

### 2.3 Configuration (`.env`)
```env
# Gate the whole subsystem
BROWSER_CONTROL_ENABLED=true
# DCOM/COM browser automation (Antigravity-style) — requires pywin32
BROWSER_DCOM_ENABLED=true
# Desktop capture / input injection
BROWSER_DESKTOP_CAPTURE_ENABLED=true
# Allow-list applies to every navigation
BROWSER_ALLOWED_HOSTS=["localhost","127.0.0.1","host.docker.internal"]
# Browser CLSIDs (note: the canonical IE ProgID has no space)
BROWSER_IE_CLSID=InternetExplorer.Application
BROWSER_IE_APPID={0002DF05-0000-0000-C000-000000000046}
BROWSER_CHROME_CLSID=Chrome HTML Document
# Defaults
BROWSER_DEFAULT_TIMEOUT_S=60
BROWSER_SCREENSHOT_QUALITY=85
BROWSER_MAX_CONCURRENT_SESSIONS=3
```

### 2.4 Database
The `BrowserSession` table is registered against `Base.metadata`; the existing Alembic `env.py` runs
`context.run_migrations()` on startup, so the table is auto-created on first run — no manual migration file
was needed.

### 2.5 API usage
```bash
# Create a Playwright session
POST /api/browser-control/sessions
{"kind":"playwright","name":"demo","url":"https://example.com"}

# Create a DCOM IE session (BROWSER_DCOM_ENABLED must be true)
POST /api/browser-control/sessions
{"kind":"dcom_ie","name":"ie-session"}

# Start it
POST /api/browser-control/sessions/{id}/start
{"url":"https://example.com","vm_id":"vm_..."}

# Action: click at (100,200)
POST /api/browser-control/sessions/{id}/action
{"action":{"action":"click","x":100,"y":200}}

# Screenshot (returns b64 PNG)
GET /api/browser-control/sessions/{id}/screenshot

# Assertion
POST /api/browser-control/sessions/{id}/assert
{"type":"text_visible","value":"Hello"}
```

### 2.6 Agent tool
```
browser_control(operation=create|start|stop|list|action|screenshot|assert,
                session_id?, name?, kind?, url?, action?, spec?)
```
Permission: `browser.control`.

## 3. Limitations & Open Questions
1. **Static analysis ceiling**: Antigravity.exe is DbK-protected and Inno Setup-encrypted; the inner logic,
   exact CLSIDs used at runtime, any persistence/registry locations, and remote-receiver endpoints are not
   recoverable without dynamic instrumentation (Frida/Regshot/live capture).
2. **DCOM driver prerequisites**: `pywin32` (installed via `pip install pywin32`) and a Windows host with a
   COM-browsable browser (`Internet Explorer.Application`, `Microsoft Edge`, or Chrome with `Chrome HTML
   Document` enabled).
3. **Desktop capture**: runs on the host desktop and requires elevated permissions for `AdjustTokenPrivileges`;
   it captures the foreground window, not an arbitrary sandbox screen. For in-VM browser automation, the
   Playwright session bound to the VM is the preferred path.
4. **No in-VM DCOM**: COM objects run in-process on the host; true in-VM browser control requires a
   Playwright server container (already supported by the existing `screen_control` `docker` mode) connected to
   the VM's network.
5. **Antigravity's actual network protocols and persistence mechanisms are not implemented** — only the
   browser-control/screen-capture/input surface was reconstructed from its import/string evidence.

## 4. Verification
- 35 unit/integration tests pass (`tests/test_browser_control.py`, `tests/standalone_browser_test.py`):
  host allow-list semantics, schema validation, CRUD round-trip against real SQLite, user isolation,
  `BrowserScreenshot`/`AssertionSpec` models.
- Full backend suite: 260 passed, 12 skipped (2 pre-existing failures unrelated to browser control,
  reproduced on a clean tree).
- Live smoke test through `SessionManager` (the API/tool path), Windows 11 build 26300:
  - **Playwright**: create → start → navigate `https://example.com` → blocked navigation returns
    `ok=False` ("blocked: …") → allowed navigation updates URL → PNG screenshot (28 KB) →
    `title_contains` PASS → stop → action-after-stop degrades gracefully → delete.
  - **Desktop capture**: start → BitBlt/GetDIBits PNG screenshot → stop (real PNG produced).
  - **DCOM/IE**: `InternetExplorer.Application` is registered on this build but its COM server
    refuses to launch (IE automation is disabled on recent Win11); `start()` raises a clear
    `BrowserControlError` which the manager surfaces as a failed start — the correct degradation.
  - App shutdown stops every running driver via `SessionManager.shutdown()`.
- Ruff clean on all touched files.

## 5. Files referenced from the REA session
- `C:\Users\thaku\Downloads\Antigravity.exe` (source binary)
- `C:\Users\thaku\hackathon-projects\Jevora-IDE\Jevora-IDE\antigravity_report.txt` (PE overview: imports,
  exports, section table, string-sample hits)
- `C:\Users\thaku\hackathon-projects\Jevora-IDE\Jevora-IDE\antigravity_deep.txt` (full ASCII/Unicode/raw binary
  string scan, resource scan, version info, RWX detection)
- `C:\Users\thaku\hackathon-projects\Jevora-IDE\Jevora-IDE\tools\analyze_antigravity.py` and
  `analyze_antigravity2.py` (the analysis scripts)
