#!/usr/bin/env python3
"""Generate Zipcatcher app icons from static/images/logo.svg.

The in-app logo is landscape (200x50) and uses white text on the dark
page background, so we wrap it in a 1024x1024 deep-space canvas before
rendering and then fan it out into .icns / .ico / .png assets.

Run:  .venv/bin/python electron/assets/build_app_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SRC_SVG = REPO / "static" / "images" / "logo.svg"
OUT_ICNS = HERE / "icon.icns"
OUT_ICO = HERE / "icon.ico"
OUT_PNG = HERE / "icon.png"

# Square canvas around the logo.svg symbol (moon + NE-pointing jet).
# The wordmark is dropped — text is unreadable at dock/finder sizes —
# but the moon+jet silhouette is the recognisable "look" of the logo.
# Source symbol occupies (5,5)-(45,45) in logo.svg viewBox coords;
# here we scale it to fill ~70% of a 200x200 square on a deep-space bg.
SQUARE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="bg" cx="38%" cy="40%" r="80%">
      <stop offset="0%"  stop-color="#0d1533"/>
      <stop offset="55%" stop-color="#07102a"/>
      <stop offset="100%" stop-color="#020612"/>
    </radialGradient>
    <radialGradient id="moonGradient" cx="50%" cy="30%">
      <stop offset="0%"  stop-color="#f0f0f0"/>
      <stop offset="70%" stop-color="#b0b0b0"/>
      <stop offset="100%" stop-color="#808080"/>
    </radialGradient>
  </defs>

  <rect width="200" height="200" fill="url(#bg)"/>

  <!-- Scale 25,25-centered symbol (radius 20) to a ~140px disk centered on (100,100). -->
  <g transform="translate(100,100) scale(3.2) translate(-25,-25)">
    <circle cx="25" cy="25" r="20" fill="url(#moonGradient)" stroke="#a0a0a0" stroke-width="0.5"/>
    <circle cx="20" cy="20" r="3"  fill="#d0d0d0" opacity="0.4"/>
    <circle cx="28" cy="27" r="2"  fill="#d0d0d0" opacity="0.4"/>
    <circle cx="23" cy="30" r="1.5" fill="#d0d0d0" opacity="0.4"/>

    <g fill="#000000" opacity="0.92" transform="rotate(45 25 25)">
      <rect x="23.5" y="14" width="3" height="22" rx="1.5"/>
      <polygon points="25,14 23.5,18 26.5,18"/>
      <polygon points="10,27 23.5,23 23.5,27 12,29"/>
      <polygon points="40,27 26.5,23 26.5,27 38,29"/>
      <polygon points="20,35 23.5,33 23.5,35"/>
      <polygon points="30,35 26.5,33 26.5,35"/>
    </g>
  </g>
</svg>
"""


def render_svg_to_png(svg_path: Path, out_png: Path, size: int) -> None:
    """Use qlmanage to rasterize an SVG at the requested size."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        subprocess.run(
            ["qlmanage", "-t", "-s", str(size), "-o", str(tmp_dir), str(svg_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        produced = next(tmp_dir.glob("*.png"))
        img = Image.open(produced).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        img.save(out_png)


def build_icns(master_png: Path) -> None:
    """Fan out master PNG into an .iconset and run iconutil."""
    sizes = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        master = Image.open(master_png).convert("RGBA")
        for name, px in sizes:
            resized = master.resize((px, px), Image.LANCZOS)
            resized.save(iconset / name)
        subprocess.run(
            ["iconutil", "-c", "icns", "-o", str(OUT_ICNS), str(iconset)],
            check=True,
        )


def build_ico(master_png: Path) -> None:
    master = Image.open(master_png).convert("RGBA")
    master.save(
        OUT_ICO,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def main() -> int:
    if not SRC_SVG.exists():
        print(f"logo not found: {SRC_SVG}", file=sys.stderr)
        return 1
    if shutil.which("qlmanage") is None:
        print("qlmanage not found (macOS only)", file=sys.stderr)
        return 1

    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(SQUARE_SVG)
        square_svg = Path(f.name)

    try:
        render_svg_to_png(square_svg, OUT_PNG, 1024)
    finally:
        square_svg.unlink(missing_ok=True)

    build_icns(OUT_PNG)
    build_ico(OUT_PNG)

    print(f"wrote {OUT_PNG}")
    print(f"wrote {OUT_ICNS}")
    print(f"wrote {OUT_ICO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
