"""Static analysis of Antigravity.exe (PE binary) - reverse engineering aid.

Outputs a structured report of the binary's structure, imports, strings,
and any browser-control (DCOM) / virtual-machine behavior.
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
from pathlib import Path

import pefile

OUT = Path("antigravity_report.txt")
TARGET = Path("C:\\Users\\thaku\\Downloads\\Antigravity.exe")

BROWSER_CONTROL_TERMS = [
    "dcom", "DCOM", "ole", "OLE", "activex", "activex", "activex",
    "iexplore", "ieframe", "microsoft", "explorer.exe", "chrome", "chromium",
    "firefox", "edge", "browser", "webview", "webview2", "cef", "chromium",
    "selenium", "playwright", " puppeteer", "vnc", "vnc", "rdp", "desktop",
    "mouse", "keyboard", "hook", "keyboard", "SetWindowsHook", "mouse", "input",
    "CreateRemoteThread", "WriteProcess", "VirtualAllocEx", "NtUnmapView", "vm", "vmx",
    "vmware", "virtualbox", "qemu", "kvm", "vmware", "hyper-v", "VirtualMachine",
    "screencap", "screen capture", "d3d9", "dxgi", "GetDC", "BitBlt", "PrintWindow",
]

WINAPI_HOOKS = [
    "SetWindowsHookEx", "GetAsyncKeyState", "GetKeyboardState", "keybd_event",
    "mouse_event", "SendInput", "GetForegroundWindow", "GetWindowThreadProcessId",
    "FindWindow", "EnumWindows", "IsWindowVisible", "GetWindowText", "GetDC",
    "CreateDC", "StartScanCode", "SetWindowsHookExA", "SetWindowsHookExW",
    "MapVirtualKey", "ToAscii", "GetKeyboardType", "GetKeyState",
]

DCOM_TERMS = ["CLSID_", "IID_", "CoInitialize", "CoCreateInstance", "CoGetObject",
              "OleInitialize", "OleRun", "CoGetContextToken", "IDispatch",
              "IWebBrowser2", "IHTMLDocument2", "IAccessible", "WebBrowser",
              "InternetExplorer.Application", "WScript.Shell", "Shell.Application"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def dump_imports(pe):
    out = []
    out.append("=== DLL IMPORTS ===")
    for dll in pe.DIRECTORY_ENTRY_IMPORT:
        out.append(f"[{dll.dll.upper()}]")
        for imp in dll.imports:
            if imp.name:
                out.append(f"  @{hex(imp.address)}: {imp.name}")
            else:
                out.append(f"  @{hex(imp.address)}: (ordinal {imp.ordinal})")
    return out


def dump_exports(pe):
    out = ["=== EXPORTS ==="]
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if exp.name:
                out.append(f"  @{hex(exp.address)}: {exp.name}")
    return out


def find_strings(pe):
    """Extract printable ASCII strings of length >= 4, skipping PE header artifacts."""
    strings = []
    for section in pe.sections:
        data = section.get_data()
        for m in re.finditer(rb"[\x20-\x7e]{4,}", data):
            s = m.group(0).decode("ascii", errors="ignore")
            # avoid repeated PE metadata artifacts
            if any(k in s for k in [".text", ".rdata", ".data", ".rsrc", ".reloc"]):
                continue
            if s.strip().startswith(".") and len(s) < 8:
                continue
            # filter trivial repeated bytes
            if len(set(s)) < 2 and len(s) > 4:
                continue
            strings.append((section.Name.decode("ascii", errors="ignore").strip("\0"),
                            s, m.start()))
    return strings


def find_embedded_bytes(pe):
    """Find non-string embedded blobs (e.g., scripts, payloads) in sections."""
    embedded = []
    for i, section in enumerate(pe.sections):
        data = section.get_data()
        name = section.Name.decode("ascii", errors="ignore").strip("\0")
        # look for recognizable embedded markers
        for marker, typ in [
            (b"<!DOCTYPE", "html"),
            (b"<html", "html"),
            (b"<iframe", "html"),
            (b"window.location", "js"),
            (b"document.write", "js"),
            (b'{"type":"browser"', "json"),
            (b"base64", "base64"),
            (b"exec(", "python"),
            (b"import ", "python"),
            (b"from ", "python"),
            (b"chrome-extension", "extension"),
        ]:
            if marker in data:
                embedded.append((name, typ, data.index(marker)))
    return embedded


def analyze():
    pe = pefile.PE(str(TARGET), fast_load=False)

    lines = []
    lines.append(f"TARGET: {TARGET}")
    lines.append(f"SHA256: {sha256(TARGET)}")
    lines.append(f"SIZE: {TARGET.stat().st_size:,} bytes")
    lines.append(f"ENTRY: 0x{pe.OPTIONAL_HEADER.AddressOfEntryPoint:x}")
    lines.append(f"MAGIC: {struct.unpack('<H', pe.header[:2])[0]:04x}")
    machine_map = {0x14c: "i386", 0x8664: "AMD64", 0x1c0: "ARM", 0xaa64: "ARM64"}
    lines.append(f"Machine: {machine_map.get(pe.FILE_HEADER.Machine, hex(pe.FILE_HEADER.Machine))}")
    subsys_map = {0: "IMAGE_SUBSYSTEM_NATIVE", 2: "IMAGE_SUBSYSTEM_WINDOWS_GUI",
                  3: "IMAGE_SUBSYSTEM_WINDOWS_CUI", 9: "IMAGE_SUBSYSTEM_WINDOWS_CE_GUI"}
    lines.append(f"Subsystem: {subsys_map.get(pe.OPTIONAL_HEADER.Subsystem, hex(pe.OPTIONAL_HEADER.Subsystem))}")
    lines.append(f"Timestamp: {pe.FILE_HEADER.TimeDateStamp} ({__import__('time').ctime(pe.FILE_HEADER.TimeDateStamp)})")
    lines.append(f"Sections: {len(pe.sections)}")
    lines.append(f"Import directories: {len(pe.DIRECTORY_ENTRY_IMPORT)}")
    lines.append(f"Resource directory present: {hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE')}")

    lines.append("")
    lines += dump_imports(pe)
    lines.append("")
    lines += dump_exports(pe)

    # strings analysis
    lines.append("")
    lines.append("=== STRING ANALYSIS (browser-control / DCOM / VM related) ===")
    strings = find_strings(pe)
    matched = []
    for sec, s, off in strings:
        s_lower = s.lower()
        if any(term in s_lower for term in [t.lower() for t in (BROWSER_CONTROL_TERMS + WINAPI_HOOKS + DCOM_TERMS)]):
            matched.append((sec, s, off))
    for sec, s, off in matched:
        lines.append(f"[{sec}] 0x{off:06x}: {s[:200]}")

    # count all strings by section for context
    from collections import Counter
    sec_counts = Counter(s for sec, s, off in strings)
    lines.append("")
    lines.append("=== STRING COUNTS BY SECTION (top 30) ===")
    for sec, c in sec_counts.most_common(30):
        lines.append(f"  {sec}: {c}")

    # embedded content detection
    lines.append("")
    lines.append("=== EMBEDDED CONTENT CANDIDATES ===")
    embedded = find_embedded_bytes(pe)
    for name, typ, off in embedded:
        lines.append(f"  section '{name}': {typ} at 0x{off}")
    # also dump sections with high entropy / large data
    lines.append("")
    lines.append("=== SECTION DETAILS (size/virtualsize/entropy-ish) ===")
    for section in pe.sections:
        data = section.get_data()
        lines.append(f"  {section.Name.decode('ascii',errors='ignore').strip(chr(0)):12s} "
                     f"raw=0x{section.PointerToRawData:06x} size=0x{section.SizeOfRawData:06x} "
                     f"virt=0x{section.VirtualAddress:06x} virtsize=0x{section.Misc_VirtualSize:06x} "
                     f"flags={section.Characteristics}")

    # check for known packer signatures
    lines.append("")
    lines.append("=== PACKER / TRASH signatures ===")
    for name, sig, start in [
        ("UPX", b"UPX!", 0),
        ("VMProtect", b"\x78\x9C", 0),
        ("Themida", b"", 0),
        ("Enigma", b"", 0),
    ]:
        with TARGET.open("rb") as fh:
            head = fh.read(min(8, TARGET.stat().st_size))
        if name == "UPX" and head.startswith(b"UPX!"):
            lines.append(f"  packed with UPX")
    # check UPX version
    upx = re.search(rb"UPX! (\d+)\.(\d+)\.(\d+)", TARGET.read_bytes())
    if upx:
        lines.append(f"  UPX version: {upx.group(1)}.{upx.group(2)}.{upx.group(3)}")

    # check for embedded PE/ZIP
    raw = TARGET.read_bytes()
    if raw.startswith(b"PK\x03\x04"):
        lines.append("  embedded ZIP/Office/OCX archive detected (PK signature)")
    if raw[0:2] == b"MZ":
        lines.append("  embedded PE header at offset 0")

    # string count total
    lines.append("")
    lines.append(f"Total printable strings found: {len(strings)}")

    with OUT.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"Report written to {OUT}")
    print("\n".join(lines))


if __name__ == "__main__":
    analyze()
