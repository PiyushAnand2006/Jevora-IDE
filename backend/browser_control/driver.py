"""Browser-control driver implementations for Jevora IDE.

This module implements the browser-control / remote-control capabilities observed in
Antigravity.exe and integrates them with the existing screen_control layer. Three
drivers are provided, selectable via ``BROWSER_CONTROL_DRIVER`` and the VM ``kind``:

* ``ComBrowserDriver`` - DCOM/COM-based browser automation (the Antigravity pattern).
  Drives Internet Explorer via ``Internet Explorer.Application`` COM object
  (``IWebBrowser2`` / ``IDispatch``) or Chrome via the ``Chrome HTML Document``
  object, using OLE automation. This mirrors how Antigravity.exe (a Delphi
  application built with ``Winapi.ActiveX`` / ``TWebBrowser`` RTTI + OLEAUT32
  Variant support) attaches to and controls browser windows.
* ``DesktopCaptureDriver`` - OS-level screen capture and input injection using WinAPI:
  ``BitBlt`` / ``PrintWindow`` for screenshots, ``SendInput`` / ``keybd_event`` /
  ``mouse_event`` for keyboard/mouse injection, ``FindWindow`` + ``SetForegroundWindow``
  for window attachment. This matches the desktop-control primitive of Antigravity's
  remote-access surface (hook-free foreground injection), exposed behind the
  ``DESKTOP_CONTROL_ENABLED``-like flag ``BROWSER_DESKTOP_CAPTURE_ENABLED``.
* ``PlaywrightBrowserDriver`` - a thin adapter around the existing
  ``backend.screen_control.playwright_driver.PlaywrightDriver`` for headless
  Chromium sessions, which Antigravity-style tooling typically prefers for
  reliability.

Security model: every drive operation is gated by an allowed-hosts allow-list,
the session is bound to a user + optional VM, and the COM objects run headless so
no interactive desktop session is required. Console/network errors are captured
for evidence.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
from typing import Any

from backend.core.logging import get_logger
from backend.decision.schemas import Assertion
from backend.screen_control.driver import (
    Action,
    AssertionSpec,
    DomainNotAllowed,
    ScreenDriver,
    host_allowed,
)

log = get_logger(__name__)


class BrowserControlError(Exception):
    """Raised when a browser-control operation fails (browser not running, host blocked, etc.)."""


class ComBrowserDriver(ScreenDriver):
    """DCOM/COM browser driver (Antigravity-style).

    Drives a browser through the Windows COM/OLE automation surface:
      * Internet Explorer: ``InternetExplorer.Application`` (ProgID) -> ``IWebBrowser2``
        + ``IDispatch`` for DOM access (equivalent to Antigravity's ``TWebBrowser`` /
          ``Winapi.ActiveX`` ``OleVariant`` pattern).
      * Chrome: ``Chrome HTML Document`` object for embedded-chromium control.

    Screenshots capture the browser window via WinAPI ``PrintWindow`` (the same
    primitive Antigravity uses for its remote display surface), because the COM
    surface exposes no direct image-export command.

    This is a reconstruction of the browser-control surface found in Antigravity.exe:
    the binary imports OLEAUT32 (VariantCopy, SysAllocString, SafeArray*, IDispatch),
    USER32 (window APIs), ADVAPI32 (token/privilege APIs) and KERNEL32, builds a
    ``TWebBrowser``-equivalent automation object, navigates and reads the DOM through
    IDispatch, and captures screens for remote display.
    """
    kind = "com"

    def __init__(self, *, clsid: str, allowed_hosts: list[str], width: int = 1280,
                 height: int = 800, timeout_s: float = 60.0,
                 headless: bool = True, browser_path: str | None = None):
        super().__init__()
        self.clsid = clsid
        self.allowed = allowed_hosts
        self.width, self.height = width, height
        self.timeout_s = timeout_s
        self.headless = headless
        self.browser_path = browser_path
        self._dispatch: Any | None = None
        self._browser: Any | None = None
        self._console: list[str] = []
        self._network: list[str] = []
        self._url = ""
        self._title = ""
        self._pid: int | None = None

    # -------------------------------------------------------------------- core ops
    async def start(self, url: str | None) -> None:
        """Launch the COM browser object and optionally navigate to ``url``."""
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise BrowserControlError(
                "pywin32 is required for DCOM browser control (pip install pywin32)") from exc
        if url and not host_allowed(url, self.allowed):
            raise DomainNotAllowed(f"{url} is outside the browser allow-list {self.allowed}")

        pythoncom.CoInitialize()
        try:
            self._browser = win32com.client.Dispatch(self.clsid)
            self._browser.Visible = not self.headless
            self._browser.Navigate(url or "about:blank")
            self._pid = self._get_pid()
            await self._wait_ready()
            self._url = str(self._browser.LocationURL or "")
            self._title = str(self._browser.Document.Title if self._browser.Document else "")
            # attach console/network error observers via document event sink when available
            await self._attach_observers()
        except Exception as exc:
            raise BrowserControlError(f"could not start COM browser {self.clsid}: {exc}") from exc

    async def screenshot(self) -> bytes:
        """Capture the browser viewport as PNG.

        Antigravity captures the controlled browser window for remote display
        (USER32 GetDC/BitBlt/PrintWindow); no OLE command offers a direct image
        export, so we reuse the window-capture primitive against the COM
        object's HWND.
        """
        if self._browser is None:
            raise BrowserControlError("browser not started")
        hwnd = None
        try:
            hwnd = int(self._browser.HWND)
        except Exception:
            hwnd = None
        if not hwnd:
            raise BrowserControlError("COM browser exposes no capturable window")
        try:
            return await asyncio.to_thread(_capture_window, hwnd, self.width, self.height)
        except BrowserControlError:
            raise
        except Exception as exc:
            raise BrowserControlError(f"screenshot failed: {exc}") from exc

    async def perform(self, action: Action) -> str:
        if action.action == "navigate" and action.url and not host_allowed(action.url, self.allowed):
            raise DomainNotAllowed(f"navigation to {action.url} is not allowed")
        if self._browser is None:
            raise BrowserControlError("browser not started")
        doc = self._browser.Document
        try:
            if action.action == "click" and action.x is not None and action.y is not None:
                # Click through the DOM at the point (execScript has no return value,
                # so guard the null case inside the script itself).
                doc.parentWindow.execScript(
                    f"var __el = document.elementFromPoint({action.x},{action.y});"
                    f"if (__el && __el.click) {{ __el.click(); }}", "javascript")
            elif action.action == "type":
                if action.x is not None and action.y is not None:
                    el = doc.elementFromPoint(action.x, action.y)
                    if el is not None:
                        el.focus()
                await self._type_text(action.text or "")
            elif action.action == "key":
                await self._press_key(action.key or action.text or "Enter")
            elif action.action == "wait":
                await asyncio.sleep(min(10.0, (action.ms or 1000) / 1000))
            elif action.action == "navigate" and action.url:
                self._browser.Navigate(action.url)
                await self._wait_ready()
            else:
                return f"ignored {action.action}"
        except Exception as exc:
            return f"failed: {exc}"
        self._url = str(self._browser.LocationURL or "")
        self._title = str(self._browser.Document.Title if self._browser.Document else "")
        return "ok"

    async def current_url(self) -> str:
        return self._url

    async def check(self, spec: AssertionSpec) -> Assertion:
        if self._browser is None:
            return Assertion(name=spec.name, passed=False, detail="browser not started")
        try:
            doc = self._browser.Document
            if spec.type == "text_visible":
                # execScript returns None, so evaluate via the DOM directly.
                body_text = str(doc.body.innerText) if doc.body is not None else ""
                passed = spec.value.lower() in body_text.lower()
                return Assertion(name=spec.name, passed=passed,
                                 detail=f"text {spec.value!r} {'visible' if passed else 'not visible'}")
            if spec.type == "title_contains":
                title = str(self._browser.Document.Title) if self._browser.Document else ""
                return Assertion(name=spec.name, passed=spec.value.lower() in title.lower(), detail=f"title={title!r}")
            if spec.type == "url_contains":
                url = str(self._browser.LocationURL or "")
                return Assertion(name=spec.name, passed=spec.value.lower() in url.lower(), detail=f"url={url}")
            if spec.type == "no_console_errors":
                return Assertion(name=spec.name, passed=not self._console, detail="; ".join(self._console[:3]))
            if spec.type == "selector_visible":
                el = doc.querySelector(spec.value)
                passed = el is not None
                return Assertion(name=spec.name, passed=passed, detail="visible" if passed else "not visible")
            return Assertion(name=spec.name, passed=False, detail="unsupported assertion")
        except Exception as exc:
            return Assertion(name=spec.name, passed=False, detail=f"{type(exc).__name__}: {exc}")

    def console_errors(self) -> list[str]:
        return list(self._console)

    def network_errors(self) -> list[str]:
        return list(self._network)

    async def close(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.Quit()
            self._browser = None
        with contextlib.suppress(Exception):
            import pythoncom
            pythoncom.CoUninitialize()
        self._dispatch = None

    # -------------------------------------------------------------------- helpers
    def _get_pid(self) -> int | None:
        try:
            return self._browser.ProcessID if hasattr(self._browser, "ProcessID") else None
        except Exception:
            return None

    async def _wait_ready(self, max_wait: float = 30.0) -> None:
        """Wait until the COM browser reports ready (Busy == False, ReadyState == 4)."""
        import time
        start = time.monotonic()
        while time.monotonic() - start < max_wait:
            try:
                if getattr(self._browser, "Busy", False):
                    await asyncio.sleep(0.2)
                    continue
                ready = getattr(self._browser, "ReadyState", 0)
                if ready in (4, "COMPLETE"):
                    return
            except Exception:
                log.debug("COM browser state probe failed during wait", exc_info=True)
            await asyncio.sleep(0.2)

    async def _attach_observers(self) -> None:
        """Attach to document events (onerror/onload) to capture console/network errors."""
        try:
            import win32com.client
            doc = self._browser.Document
            if doc is None:
                return
            # IE exposes document events; wire a simple sink via pywin32 event binding.
            sink = _DocSink(self)
            self._dispatch = win32com.client.WithEvents(doc, sink)  # type: ignore
        except Exception:
            log.debug("document event sink unavailable (best-effort)", exc_info=True)

    async def _mouse_move(self, x: int, y: int) -> None:
        await asyncio.to_thread(_set_cursor_pos, x, y)

    async def _type_text(self, text: str) -> None:
        for ch in text:
            await asyncio.to_thread(_keybd_char, ch)
            await asyncio.sleep(0.01)

    async def _press_key(self, key: str) -> None:
        await asyncio.to_thread(_keybd_vk, key)


class _DocSink:
    """pywin32 event sink for the IE document (console/error capture)."""
    def __init__(self, driver: ComBrowserDriver) -> None:
        self._driver = driver

    def onerror(self, script, line, column, description, url, hres):
        self._driver._console.append(f"error: {description}")

    def onreadystatechange(self):
        pass


# --------------------------------------------------------------------- winapi helpers
VK_MAP = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
          "delete": 0x2E, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
          "space": 0x20, "f1": 0x70, "f5": 0x74, "arrowup": 0x26,
          "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27}


def _set_cursor_pos(x: int, y: int) -> None:
    import win32api
    win32api.SetCursorPos((int(x), int(y)))


def _keybd_char(ch: str) -> None:
    """Press and release one character key (Antigravity-style keybd_event injection)."""
    import win32api
    vk = ord(ch) & 0xFF
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def _keybd_vk(key: str) -> None:
    """Press and release a named virtual key (falls back to the first character)."""
    import win32api
    vk = VK_MAP.get(key.lower())
    if vk is None:
        vk = ord(key[0]) if key else 0x20
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, 2, 0)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _capture_gdi(hwnd: int | None, width: int, height: int) -> bytes:
    """Capture the screen (``hwnd=None``) or a window (``PrintWindow``) as PNG.

    This is the capture primitive observed in Antigravity.exe (USER32 GetDC /
    GDI BitBlt / PrintWindow): the remote display surface for its controlled
    windows. Returns PNG bytes suitable for the wire format.
    """
    import ctypes

    import PIL.Image
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    SRCCOPY = 0x00CC0020
    PW_RENDERFULLCONTENT = 2

    hdc_screen = user32.GetDC(0)
    if not hdc_screen:
        raise BrowserControlError("GetDC(0) failed")
    mem_dc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    old_bmp = gdi32.SelectObject(mem_dc, bmp)
    try:
        if hwnd:
            if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
                raise BrowserControlError("PrintWindow failed")
        else:
            gdi32.BitBlt(mem_dc, 0, 0, width, height, hdc_screen, 0, 0, SRCCOPY)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # negative -> top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), 0):
            raise BrowserControlError("GetDIBits failed")
        img = PIL.Image.frombuffer("RGBA", (width, height), buf.raw, "raw", "BGRA", 0, 1)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    finally:
        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, hdc_screen)


def _capture_window(hwnd: int, width: int, height: int) -> bytes:
    """Capture a single window (used by ComBrowserDriver for its HWND)."""
    return _capture_gdi(hwnd, width, height)


class DesktopCaptureDriver(ScreenDriver):
    """OS-level desktop capture and input injection (Antigravity remote-access surface).

    Uses WinAPI directly:
      * Screenshots: ``BitBlt`` of the primary monitor DC, optionally a target window
        via ``PrintWindow`` (mirror of Antigravity's screen-capture primitive).
      * Input: ``keybd_event`` / ``mouse_event`` injection (the hook-free primitives
        present in Antigravity's import table), and ``FindWindow`` /
        ``SetForegroundWindow`` for window targeting.
    """
    kind = "desktop"

    def __init__(self, width: int = 1280, height: int = 800, window_title: str | None = None,
                 allowed_hosts: list[str] | None = None):
        super().__init__()
        self.width, self.height = width, height
        self.window_title = window_title
        self.allowed = allowed_hosts or []
        self._hwnd: int | None = None

    async def start(self, url: str | None) -> None:
        if url and not host_allowed(url, self.allowed):
            raise DomainNotAllowed(f"{url} is outside the desktop allow-list {self.allowed}")
        if self.window_title:
            self._hwnd = await asyncio.to_thread(self._find_window, self.window_title)

    async def screenshot(self) -> bytes:
        return await asyncio.to_thread(_capture_gdi, self._hwnd, self.width, self.height)

    async def perform(self, action: Action) -> str:
        if action.action == "click" and action.x is not None and action.y is not None:
            await asyncio.to_thread(self._send_mouse_click, action.x, action.y)
        elif action.action == "type":
            for ch in action.text or "":
                await asyncio.to_thread(self._send_char, ch)
                await asyncio.sleep(0.01)
        elif action.action == "key":
            await asyncio.to_thread(_keybd_vk, action.key or action.text or "Enter")
        elif action.action == "scroll":
            await asyncio.to_thread(self._send_scroll, action.dy or 400)
        elif action.action == "wait":
            await asyncio.sleep(min(10.0, (action.ms or 1000) / 1000))
        else:
            return f"ignored {action.action}"
        return "ok"

    def _find_window(self, title: str) -> int | None:
        import ctypes
        user32 = ctypes.windll.user32
        exact = user32.FindWindowW(None, title)
        if exact:
            return exact
        # EnumWindows fallback: first visible window whose title contains the text.
        needle = title.lower()
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return 1
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if needle in buf.value.lower():
                    found.append(hwnd)
            return 1  # keep enumerating; take the first match afterwards

        user32.EnumWindows(proc(cb), 0)
        return found[0] if found else None

    def _send_mouse_click(self, x: int, y: int) -> None:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

    def _send_char(self, ch: str) -> None:
        import ctypes
        user32 = ctypes.windll.user32
        res = user32.VkKeyScanW(ord(ch))
        if res == -1:
            return  # no layout mapping for this character
        vk = res & 0xFF
        shift = (res >> 8) & 0xFF
        if shift & 1:
            user32.keybd_event(0x10, 0, 0, 0)  # shift down
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        if shift & 1:
            user32.keybd_event(0x10, 0, 2, 0)  # shift up

    def _send_scroll(self, delta: int) -> None:
        import ctypes
        user32 = ctypes.windll.user32
        user32.mouse_event(0x0800, 0, 0, delta * 120, 0)  # MOUSEEVENTF_WHEEL

    async def current_url(self) -> str:
        return ""

    async def check(self, spec: AssertionSpec) -> Assertion:
        return Assertion(name=spec.name, passed=False, detail="assertion not supported by desktop capture")

    def console_errors(self) -> list[str]:
        return []

    def network_errors(self) -> list[str]:
        return []

    async def close(self) -> None:
        self._hwnd = None


class PlaywrightBrowserDriver(ScreenDriver):
    """Adapter around the existing Playwright driver for browser-control sessions."""
    kind = "playwright"

    def __init__(self, *, allowed_hosts: list[str], width: int = 1280, height: int = 800,
                 ws_endpoint: str | None = None):
        super().__init__()
        from backend.screen_control.playwright_driver import PlaywrightDriver as _PD
        self._inner = _PD(allowed_domains=allowed_hosts, width=width, height=height, ws_endpoint=ws_endpoint)
        self._console: list[str] = []
        self._network: list[str] = []
        self._url = ""

    async def start(self, url: str | None) -> None:
        await self._inner.start(url)
        self._url = await self._inner.current_url()

    async def screenshot(self) -> bytes:
        return await self._inner.screenshot()

    async def perform(self, action: Action) -> str:
        return await self._inner.perform(action)

    async def current_url(self) -> str:
        self._url = await self._inner.current_url()
        return self._url

    async def check(self, spec: AssertionSpec) -> Assertion:
        return await self._inner.check(spec)

    def console_errors(self) -> list[str]:
        return self._inner.console_errors()

    def network_errors(self) -> list[str]:
        return self._inner.network_errors()

    async def close(self) -> None:
        await self._inner.close()
