#!/usr/bin/env python3
"""Generate electron/assets/dmg-background.png from the splash assets.

Run with the project venv:  .venv/bin/python electron/assets/build_dmg_background.py

The DMG layout places the app icon at (130, 220) and the /Applications
alias at (410, 220), so the lower-center strip of this background must
stay visually clear.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 660, 400
HERE = Path(__file__).resolve().parent
OUT = HERE / "dmg-background.png"
MOON_SRC = HERE / "moon.png"
AIRCRAFT_SRC = HERE / "aircraft-737.png"


def deep_space_background(w: int, h: int) -> Image.Image:
    # Vertical radial-ish gradient from deep navy at top-left to near-black.
    base = Image.new("RGB", (w, h), (3, 8, 16))
    top = Image.new("RGB", (w, h), (13, 21, 51))
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    cx, cy = int(w * 0.38), int(h * 0.40)
    max_r = int((w ** 2 + h ** 2) ** 0.5)
    for r in range(max_r, 0, -8):
        alpha = max(0, 255 - int(255 * r / max_r * 1.3))
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=alpha)
    return Image.composite(top, base, mask)


def add_stars(img: Image.Image, count: int = 180) -> None:
    import random
    rng = random.Random(1337)
    draw = ImageDraw.Draw(img, "RGBA")
    for _ in range(count):
        x = rng.randint(0, W - 1)
        y = rng.randint(0, H - 1)
        # Avoid the moon area and the drop zone strip.
        if x > 430 and y < 220:
            continue
        if 80 <= y <= 330 and 90 <= x <= 560:
            if rng.random() < 0.5:
                continue
        r = rng.choice([0, 0, 0, 1, 1, 2])
        a = rng.randint(90, 220)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, a))


def paste_with_glow(canvas: Image.Image, src: Image.Image, box, glow_color, glow_radius: int) -> None:
    x, y, w, h = box
    src = src.resize((w, h), Image.LANCZOS).convert("RGBA")
    # Build a glow layer from the alpha channel.
    alpha = src.split()[-1]
    glow = Image.new("RGBA", (w + glow_radius * 4, h + glow_radius * 4), (0, 0, 0, 0))
    glow_mask = Image.new("L", glow.size, 0)
    glow_mask.paste(alpha, (glow_radius * 2, glow_radius * 2))
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(glow_radius))
    glow_rgba = Image.new("RGBA", glow.size, glow_color)
    glow_rgba.putalpha(glow_mask)
    canvas.alpha_composite(glow_rgba, (x - glow_radius * 2, y - glow_radius * 2))
    canvas.alpha_composite(src, (x, y))


def load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Futura.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    canvas = deep_space_background(W, H).convert("RGBA")
    add_stars(canvas)

    moon = Image.open(MOON_SRC).convert("RGBA")
    aircraft = Image.open(AIRCRAFT_SRC).convert("RGBA")

    # Moon — top-right, slightly off-canvas top for the limb-glow look.
    paste_with_glow(
        canvas,
        moon,
        box=(W - 220 + 20, -30, 220, 220),
        glow_color=(120, 170, 255, 120),
        glow_radius=18,
    )

    # 737 — upper-left, raised so it doesn't crowd the wordmark.
    paste_with_glow(
        canvas,
        aircraft,
        box=(40, -10, 260, 260),
        glow_color=(0, 0, 0, 0),  # shadow, not glow
        glow_radius=8,
    )

    draw = ImageDraw.Draw(canvas)

    # Wordmark — centered horizontally above the drop zone.
    title_font = load_font(54)
    sub_font = load_font(16)

    title = "ZIPCATCHER"
    tw = draw.textlength(title, font=title_font)
    tx = (W - tw) / 2
    ty = 120
    # Soft glow behind the title.
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.text((tx, ty), title, font=title_font, fill=(100, 170, 255, 140))
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    canvas.alpha_composite(glow)
    draw.text((tx, ty), title, font=title_font, fill=(235, 245, 255, 255))

    sub = "AIRCRAFT  TRANSIT  TRACKER"
    sw = draw.textlength(sub, font=sub_font)
    draw.text(((W - sw) / 2, ty + 64), sub, font=sub_font, fill=(104, 144, 184, 255))

    # Hint text beneath the drop zone ("Drag to Applications").
    hint_font = load_font(14)
    hint = "drag  →  to install"
    hw = draw.textlength(hint, font=hint_font)
    draw.text(((W - hw) / 2, 330), hint, font=hint_font, fill=(90, 110, 140, 255))

    canvas.convert("RGB").save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
