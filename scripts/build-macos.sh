#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="$ROOT/release"
SPEC_DIR="$ROOT/pyi-specs"
ICON_SVG="$ROOT/assets/bonsai-app-icon.svg"
ICON_PNG="$ROOT/assets/bonsai-app-icon-1024.png"
ICONSET_DIR="$ROOT/assets/bonsai-app-icon.iconset"
ICON_ICNS="$ROOT/assets/bonsai-app-icon.icns"
TMP_ROOT="$(mktemp -d /tmp/bonsai-release.XXXXXX)"
TMP_APP="$TMP_ROOT/BonsaiChat.app"
DMG_STAGE="$TMP_ROOT/dmg-stage"

mkdir -p "$RELEASE_DIR" "$SPEC_DIR"
rm -rf "$ROOT/build-onefile-v2" "$ROOT/build-app-v2"
trap 'rm -rf "$TMP_ROOT"' EXIT

clear_xattrs() {
  for path in "$@"; do
    if [ -e "$path" ]; then
      xattr -cr "$path" 2>/dev/null || true
    fi
  done
}

if [ -f "$ICON_SVG" ]; then
  rm -f "$ICON_PNG" "$ROOT/assets/bonsai-app-icon.svg.png"
  rm -rf "$ICONSET_DIR"
  qlmanage -t -s 1024 -o "$ROOT/assets" "$ICON_SVG" >/dev/null 2>&1
  mv -f "$ROOT/assets/bonsai-app-icon.svg.png" "$ICON_PNG"
  mkdir -p "$ICONSET_DIR"

  icon_png() {
    local size="$1"
    local name="$2"
    sips -z "$size" "$size" "$ICON_PNG" --out "$ICONSET_DIR/$name" >/dev/null
  }

  icon_png 16 icon_16x16.png
  icon_png 32 icon_16x16@2x.png
  icon_png 32 icon_32x32.png
  icon_png 64 icon_32x32@2x.png
  icon_png 128 icon_128x128.png
  icon_png 256 icon_128x128@2x.png
  icon_png 256 icon_256x256.png
  icon_png 512 icon_256x256@2x.png
  icon_png 512 icon_512x512.png
  icon_png 1024 icon_512x512@2x.png

  iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
  clear_xattrs "$ICON_PNG" "$ICONSET_DIR" "$ICON_ICNS"
fi

ICON_ARGS=()
if [ -f "$ICON_ICNS" ]; then
  ICON_ARGS+=(--icon "$ICON_ICNS")
fi

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name BonsaiChat-mac \
  --distpath "$RELEASE_DIR" \
  --workpath "$ROOT/build-onefile-v2" \
  --specpath "$SPEC_DIR" \
  --add-data "$ROOT/bonsai-chat.html:." \
  "${ICON_ARGS[@]}" \
  --hidden-import huggingface_hub \
  --collect-all huggingface_hub \
  "$ROOT/bonsai.py"

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name BonsaiChat \
  --distpath "$RELEASE_DIR" \
  --workpath "$ROOT/build-app-v2" \
  --specpath "$SPEC_DIR" \
  --add-data "$ROOT/bonsai-chat.html:." \
  "${ICON_ARGS[@]}" \
  --hidden-import huggingface_hub \
  --collect-all huggingface_hub \
  "$ROOT/bonsai.py"

clear_xattrs "$RELEASE_DIR/BonsaiChat-mac"
rm -rf "$TMP_APP" "$DMG_STAGE"
ditto "$RELEASE_DIR/BonsaiChat.app" "$TMP_APP"
clear_xattrs "$TMP_APP"
codesign --force --deep --sign - "$TMP_APP"
codesign --verify --deep --strict --verbose=2 "$TMP_APP"

mkdir -p "$DMG_STAGE"
cp -R "$TMP_APP" "$DMG_STAGE/"
hdiutil create \
  -volname BonsaiChat \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$RELEASE_DIR/BonsaiChat.dmg"

echo "Artifacts written to $RELEASE_DIR"
