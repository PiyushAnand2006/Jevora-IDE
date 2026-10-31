"""Deep static analysis of Antigravity.exe - strings, resources, config.

Focused on uncovering browser-control / DCOM / VM behavior hidden by Delphi
obfuscation. Looks for: encoded/encrypted strings, registry keys, URLs,
command lines, class names of COM objects, VM/hardware checks.
"""
from __future__ import annotations

import base64
import json
import re
import struct
import sys
from pathlib import Path

import pefile

TARGET = Path("C:\\Users\\thaku\\Downloads\\Antigravity.exe")
OUT = Path("antigravity_report2.txt")

# Delphi VCL/ActiveX class names that indicate COM automation of browser apps
BROWSER_CLASSES = [
    "TWebBrowser", "THTMLDocument", "OEXHTMLDocument", "SHDocVw", "Internet Explorer.Application",
    "InternetExplorer.Application", "WebBrowser", "TInternetExplorer", "MSHTML", "TWebBrowser80",
    "TWebBrowser70", "TWebBrowser60", "TWebBrowser50", "TWebBrowser40",
]

# Registry paths commonly used for browser/automation setup
REG_KEYS = [
    "Software\\Microsoft\\Internet Explorer", "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths",
    "Software\\Classes\\CLSID", "Software\\Classes\\Interface", "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE",
]

# Hardware/VM detection API / heuristics
HW_TERMS = ["SerialNumber", "MachineGUID", "MAC", "AdapterAddress", "Processor", "VMware", "VMX",
            "VirtualBox", "VBox", "QEMU", "HyperV", "KVM", "BiosVersion", "Chassis", "Disk",
            "C:\\", "D:\\", "WindowsDirectory", "SystemDirectory"]

# Known Delphi string encryption: "TStrings" + resource handling patterns
def read_wide(path: Path, start: int, length: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(length)


def analyze():
    with TARGET.open("rb") as fh:
        raw = fh.read()
    pe = pefile.PE(str(TARGET), fast_load=False)

    lines = []
    lines.append("=== RAW BINARY STRING SCAN ===")
    # All ASCII strings >= 6
    ascii_re = re.compile(rb"[\x20-\x7e]{6,}")
    ascii_hits = []
    for m in ascii_re.finditer(raw):
        s = m.group(0).decode("ascii", errors="ignore")
        # filter noise
        if re.match(r"^[A-Z_]{6,}$", s) and s in ("FALSE", "TRUE", "NIL"):
            continue
        ascii_hits.append((m.start(), s))
    # keep only strings with at least one non-alpha char or length < 20, or match browser terms
    filtered = []
    for off, s in ascii_hits:
        if len(s) < 18 or any(c.islower() for c in s) or any(b in s for b in BROWSER_CLASSES + HW_TERMS + REG_KEYS):
            filtered.append((off, s))
    seen = set()
    unique = []
    for off, s in filtered:
        if s not in seen:
            seen.add(s)
            unique.append((off, s))
    unique.sort()
    lines.append(f"Unique raw ASCII hits ({len(unique)}):")
    for off, s in unique:
        lines.append(f"  0x{off:06x}: {s[:200]}")

    lines.append("")
    lines.append("=== UNICODE STRING SCAN (wide chars) ===")
    uni_re = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
    uni_hits = []
    for m in uni_re.finditer(raw):
        s = m.group(0).decode("utf-16-le", errors="ignore")
        uni_hits.append((m.start(), s))
    uni_hits = [(off, s) for off, s in uni_hits if len(s) < 120]
    seen = set()
    unique = []
    for off, s in uni_hits:
        if s not in seen:
            seen.add(s)
            unique.append((off, s))
    unique.sort()
    lines.append(f"Unique wide hits ({len(unique)}):")
    for off, s in unique:
        lines.append(f"  0x{off:06x}: {s[:200]}")

    lines.append("")
    lines.append("=== BASE64-LIKE BLOBS ===")
    b64_re = re.compile(rb"[A-Za-z0-9+/]{40,}={0,2}")
    b64_hits = []
    for m in b64_re.finditer(raw):
        b64_hits.append((m.start(), m.group(0).decode("ascii", errors="ignore")))
    seen = set()
    for off, s in b64_hits[:500]:
        if s not in seen:
            seen.add(s)
            try:
                decoded = base64.b64decode(s)
                if decoded[:4] in (b"PK\x03\x04", b"MZ\x00\x00", b"\x89PNG", b"<html"):
                    lines.append(f"  0x{off:06x}: base64 decodes to {decoded[:4]!r} ({len(decoded)} bytes)")
            except Exception:
                pass

    lines.append("")
    lines.append("=== RESOURCE SECTION SCAN ===")
    for section in pe.sections:
        data = section.get_data()
        name = section.Name.decode("ascii", errors="ignore").strip("\0")
        if name == ".rsrc":
            # scan for URLs and file paths
            for m in re.finditer(rb"[A-Za-z0-9._-]{5,}\.[A-Za-z]{2,6}([:/?=&#%_.-][A-Za-z0-9._/-]*){2,}", data):
                s = m.group(0).decode("ascii", errors="ignore")
                if s not in seen:
                    seen.add(s)
                    lines.append(f"  [{name}] 0x{m.start():06x}: {s[:250]}")
            # find any TStrings-like binary string table (count + null-terminated pairs)
            # Delphi stores strings as: count(W), then each string: length(W), chars, null
            lines.append("  Scanning for binary string tables in .rsrc...")
            # simple heuristic: look for 4-byte count at offset, followed by length-prefixed strings
            i = 0
            while i < len(data) - 4:
                cnt = struct.unpack_from("<I", data, i)[0]
                if cnt > 0 and cnt < 5000:
                    j = i + 4
                    ok = True
                    parsed = []
                    for _ in range(min(cnt, 300)):
                        if j + 2 > len(data):
                            ok = False
                            break
                        slen = struct.unpack_from("<H", data, j)[0]
                        j += 2
                        if j + slen > len(data):
                            ok = False
                            break
                        try:
                            parsed.append(data[j:j + slen].decode("utf-16-le", errors="ignore"))
                        except Exception:
                            ok = False
                            break
                        j += slen
                    if ok and parsed:
                        found_interesting = [p for p in parsed if len(p) > 5]
                        if found_interesting:
                            lines.append(f"    binary string table at 0x{i:06x}: count={cnt}, strings={[s[:150] for s in found_interesting]}")
                            seen.add(str(found_interesting))
                i += 1

    lines.append("")
    lines.append("=== VERSION / INFO STRINGS ===")
    if hasattr(pe, "VS_FIXEDFILEINFO") and pe.VS_FIXEDFILEINFO:
        vi = pe.VS_FIXEDFILEINFO[0]
        mv = getattr(vi, "MachineVersion", None)
        pv = getattr(vi, "ProductVersion", None)
        lines.append(f"  FixedVerInfo present (attr={type(vi).__name__})")
        if mv:
            lines.append(f"  MachineVersion: {mv.High}={mv.Low}")
        if pv:
            lines.append(f"  ProductVersion: {pv.High}={pv.Low}")
    lines.append(f"  StringFileInfo keys: {list(getattr(pe, 'StringFileInfo', {}).keys())}")
    try:
        for k in ["Comments", "CompanyName", "ProductName", "FileDescription", "LegalCopyright"]:
            try:
                val = pe.StringFileInfo[k]
                lines.append(f"  {k}: {val.decode('utf-8', errors='ignore')!r}")
            except Exception:
                pass
    except Exception as e:
        lines.append(f"  StringFileInfo error: {e}")

    lines.append("")
    lines.append("=== MEMORY-PROTECTION/EXEC-FLAG SCAN (find executable sections with data) ===")
    # OEP detection: sections with both write and exec (RWX) suggest unpacking stub
    for section in pe.sections:
        fl = section.Characteristics
        write = bool(fl & 0x80000000)
        execf = bool(fl & 0x20000000)
        read = bool(fl & 0x40000000)
        if (write and execf) or (execf and read and not write):
            lines.append(f"  {section.Name.decode('ascii',errors='ignore').strip(chr(0))}: flags={hex(fl)} (RWX/RXE -> likely unpacked stub or packed)")
    # check TLS callback / exception handler entries that jump outside .text (unpacking)
    lines.append("")
    lines.append("=== RICH HEADER (if present) ===")
    if raw[:4] == b"Rich":
        d = raw[4:]
        (key,) = struct.unpack_from("<I", d, 0)
        (count,) = struct.unpack_from("<I", d, 8)
        lines.append(f"  Rich header key=0x{key:08x} count={count}")

    safe_lines = []
    for ln in lines:
        ln_bytes = ln.encode("utf-8", errors="replace")
        try:
            ln_bytes.decode("cp1252")
            safe_lines.append(ln)
        except UnicodeDecodeError:
            safe_lines.append(ln_bytes.decode("utf-8", errors="replace"))
    out_text = "\n".join(safe_lines) + "\n"
    with OUT.open("w", encoding="utf-8") as fh:
        fh.write(out_text)
    # write stdout as raw utf-8 bytes to avoid cp1252 encoding
    sys.stdout.buffer.write(out_text.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    analyze()
