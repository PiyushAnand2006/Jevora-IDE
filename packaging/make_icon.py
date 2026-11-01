"""Generate the Jevora IDE application icons (build/icon.ico + build/icon.png).

Renders the ORIGINAL "Jevora J + network nodes" logo — the same inline SVG that
ships in ``frontend/auth.html`` — so the desktop icon matches the product's own
branding instead of a placeholder. The SVG is rasterized with the installed
Playwright Chromium at high resolution, then packed into a multi-size ICO.

Run via:  npm run icon   (i.e. python packaging/make_icon.py)
"""
from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SIZES = (256, 128, 64, 48, 32, 16)
RENDER = 512

# Verbatim copy of the logo SVG from frontend/auth.html (the Jevora J), with
# width/height swapped for the render size and an id- prefix so gradient ids
# stay unique when embedded in a page.
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 48 48" fill="none">
  <defs>
    <linearGradient id="jLetterGrad" x1="6" y1="4" x2="22" y2="44" gradientUnits="userSpaceOnUse">
      <stop stop-color="#38BDF8"/>
      <stop offset="0.55" stop-color="#3B82F6"/>
      <stop offset="1" stop-color="#6366F1"/>
    </linearGradient>
    <linearGradient id="nodeGrad" x1="28" y1="6" x2="46" y2="36" gradientUnits="userSpaceOnUse">
      <stop stop-color="#38BDF8"/>
      <stop offset="1" stop-color="#7C3AED"/>
    </linearGradient>
  </defs>
  <rect x="8" y="4" width="16" height="5" rx="2" fill="url(#jLetterGrad)"/>
  <rect x="16" y="4" width="8" height="28" rx="2" fill="url(#jLetterGrad)"/>
  <path d="M24 30 Q24 40 16 40 Q8 40 8 33 L8 29 Q8 26 11 26 L13 26 Q13 28 13 33 Q13 35 16 35 Q19 35 19 30 Z" fill="url(#jLetterGrad)"/>
  <line x1="34" y1="10" x2="30" y2="18" stroke="#94A3B8" stroke-width="1.5" stroke-linecap="round"/>
  <line x1="34" y1="10" x2="43" y2="16" stroke="#94A3B8" stroke-width="1.5" stroke-linecap="round"/>
  <line x1="30" y1="18" x2="36" y2="26" stroke="#94A3B8" stroke-width="1.5" stroke-linecap="round"/>
  <circle cx="30" cy="18" r="4" fill="#38BDF8"/>
  <circle cx="34" cy="8" r="3.2" fill="#38BDF8"/>
  <circle cx="43" cy="16" r="2.8" fill="#7C3AED"/>
  <circle cx="36" cy="26" r="2.8" fill="#6366F1"/>
</svg>"""


def render_png(size: int) -> bytes:
    """Rasterize the logo SVG with Playwright Chromium (transparent background)."""
    from playwright.sync_api import sync_playwright

    data_url = "data:image/svg+xml;base64," + base64.b64encode(
        SVG.replace("{size}", str(size)).encode("utf-8")).decode("ascii")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
        page.goto(data_url)
        page.wait_for_timeout(150)  # gradient rasterization settle
        png = page.screenshot(omit_background=True)
        browser.close()
    return png


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    png = render_png(RENDER)
    (BUILD / "icon.png").write_bytes(png)

    import io

    from PIL import Image
    base = Image.open(io.BytesIO(png)).convert("RGBA")
    base.save(BUILD / "icon.ico", sizes=[(s, s) for s in SIZES])
    print(f"[icon] rendered the Jevora J logo -> {BUILD / 'icon.png'} and {BUILD / 'icon.ico'}")


if __name__ == "__main__":
    main()
