#!/usr/bin/env bash
# Build script for Zipcatcher desktop app (macOS / Linux)
# Produces: dist-electron/Zipcatcher-{version}.dmg  (macOS)
#           dist-electron/Zipcatcher-{version}.AppImage (Linux)
#
# Prerequisites:
#   macOS:  brew install python3 node imagemagick inkscape
#   Linux:  apt install python3 python3-pip nodejs npm imagemagick inkscape
#
# Usage:  ./build.sh [--skip-pyinstaller] [--skip-icons]

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

SKIP_PYI=false
SKIP_ICONS=false
for arg in "$@"; do
  [[ "$arg" == "--skip-pyinstaller" ]] && SKIP_PYI=true
  [[ "$arg" == "--skip-icons" ]] && SKIP_ICONS=true
done

echo "═══════════════════════════════════════════"
echo "  Zipcatcher build"
echo "═══════════════════════════════════════════"

# ── 1. Python environment ──────────────────────────────────────────────────
if [ ! -d .venv ]; then
  echo "→ Creating virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller

# ── 2. Generate app icons ──────────────────────────────────────────────────
if [ "$SKIP_ICONS" = false ]; then
  echo "→ Generating app icons..."
  ICON_SRC="$ROOT/static/images/logo.svg"
  ICON_DIR="$ROOT/electron/assets"
  mkdir -p "$ICON_DIR"

  # PNG (Linux + base)
  if command -v inkscape &>/dev/null; then
    inkscape "$ICON_SRC" --export-type=png --export-width=512 --export-filename="$ICON_DIR/icon.png" 2>/dev/null
  elif command -v convert &>/dev/null; then
    convert -background none "$ICON_SRC" -resize 512x512 "$ICON_DIR/icon.png"
  else
    echo "  ⚠ No inkscape/imagemagick found — skipping icon generation (add icon.png manually)"
  fi

  # macOS .icns
  if [ -f "$ICON_DIR/icon.png" ] && [ "$(uname)" = "Darwin" ]; then
    ICONSET="$ICON_DIR/icon.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
      sips -z $size $size "$ICON_DIR/icon.png" --out "$ICONSET/icon_${size}x${size}.png" &>/dev/null
      sips -z $((size*2)) $((size*2)) "$ICON_DIR/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" &>/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICON_DIR/icon.icns"
    rm -rf "$ICONSET"
    echo "  ✓ icon.icns generated"
  fi

  # Windows .ico (ImageMagick)
  if [ -f "$ICON_DIR/icon.png" ] && command -v convert &>/dev/null; then
    convert "$ICON_DIR/icon.png" -resize 256x256 "$ICON_DIR/icon.ico"
    echo "  ✓ icon.ico generated"
  fi
fi

# ── 3. PyInstaller — bundle Flask backend ─────────────────────────────────
if [ "$SKIP_PYI" = false ]; then
  echo "→ Running PyInstaller..."
  pyinstaller flymoon.spec --distpath "$ROOT/electron/assets/bin" --workpath /tmp/pyi-build --noconfirm
  # Move binary to project root so electron-builder can find it via extraResources
  mv "$ROOT/electron/assets/bin/flymoon-server" "$ROOT/flymoon-server" 2>/dev/null || true
  echo "  ✓ flymoon-server binary built"

  # ── 3a. Codesign the PyInstaller binary (macOS only) ────────────────────
  if [ "$(uname)" = "Darwin" ]; then
    # Find the signing identity — use CODESIGN_IDENTITY env var if set,
    # otherwise auto-detect a Developer ID Application certificate
    if [ -z "$CODESIGN_IDENTITY" ]; then
      CODESIGN_IDENTITY=$(security find-identity -v -p codesigning | \
        grep "Developer ID Application" | head -1 | \
        sed 's/.*"\(.*\)".*/\1/')
    fi
    if [ -n "$CODESIGN_IDENTITY" ]; then
      echo "→ Codesigning flymoon-server with: $CODESIGN_IDENTITY"
      codesign --force --options runtime --sign "$CODESIGN_IDENTITY" \
        --entitlements "$ROOT/electron/entitlements.mac.plist" \
        "$ROOT/flymoon-server"
      echo "  ✓ flymoon-server signed"
    else
      echo "  ⚠ No Developer ID Application certificate found — skipping codesign"
      echo "    Set CODESIGN_IDENTITY env var or install a certificate"
    fi
  fi
fi

# ── 4. npm install ─────────────────────────────────────────────────────────
echo "→ Installing npm dependencies..."
cd "$ROOT/electron"
npm install --save-dev electron electron-builder 2>/dev/null

# ── 5. electron-builder ───────────────────────────────────────────────────
echo "→ Building Electron app..."
if [ "$(uname)" = "Darwin" ]; then
  # electron-builder reads APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, and
  # APPLE_TEAM_ID from env for notarization. Set them in your shell or
  # .env before running build.sh.
  if [ -n "$APPLE_ID" ] && [ -n "$APPLE_TEAM_ID" ]; then
    echo "  ✓ Notarization credentials detected — will sign and notarize"
  else
    echo "  ⚠ Set APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID for notarization"
    echo "    Building unsigned DMG (right-click → Open to bypass Gatekeeper)"
  fi
  npx electron-builder --mac
else
  npx electron-builder --linux
fi

echo ""
echo "✅ Build complete! Output in: $ROOT/dist-electron/"
