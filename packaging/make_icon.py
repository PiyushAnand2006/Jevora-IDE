"""Generate the Jevora IDE application icons (build/icon.ico + build/icon.png).

Draws a geometric 'J' mark on an indigo rounded square so no font file or
network asset is required. Idempotent; safe to re-run.

Run via:  npm run icon   (i.e. python packaging/make_icon.py)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SIZES = (256, 128, 64, 48, 32, 16)

BG = (49, 46, 129, 255)      # indigo-900
ACCENT = (99, 102, 241, 255) # indigo-500
FG = (238, 242, 255, 255)    # indigo-50


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    except AttributeError:  # very old Pillow
        draw.rectangle(box, fill=fill)


def glyph(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, size - 1, size - 1), max(2, size // 6), BG)

    # geometric 'J': vertical stem + bottom hook, plus the dot above
    u = size / 16  # one unit
    stem_left = 6 * u
    stem_right = 10 * u
    stem_top = 4 * u
    hook_bottom = 12 * u
    hook_right = 12 * u
    draw.rectangle((stem_left, stem_top, stem_right, hook_bottom), fill=FG)
    draw.pieslice(
        (5 * u, hook_bottom - 4 * u, hook_right, hook_bottom + 4 * u),
        start=0, end=90, fill=FG,
    )
    draw.rectangle((5 * u, 10 * u, stem_right, hook_bottom), fill=FG)
    draw.ellipse((6 * u, 1.5 * u, 10 * u, 3.5 * u), fill=ACCENT)
    return img


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    base = glyph(256)
    base.save(BUILD / "icon.png")
    base.save(BUILD / "icon.ico", sizes=[(s, s) for s in SIZES])
    print(f"[icon] wrote {BUILD / 'icon.png'} and {BUILD / 'icon.ico'}")


if __name__ == "__main__":
    main()
