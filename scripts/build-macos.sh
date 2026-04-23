#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="$ROOT/release"
SPEC_DIR="$ROOT/pyi-specs"
DMG_STAGE="$ROOT/dmg-stage"

mkdir -p "$RELEASE_DIR" "$SPEC_DIR"
rm -rf "$ROOT/build-onefile-v2" "$ROOT/build-app-v2" "$DMG_STAGE"

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name BonsaiChat-mac \
  --distpath "$RELEASE_DIR" \
  --workpath "$ROOT/build-onefile-v2" \
  --specpath "$SPEC_DIR" \
  --add-data "$ROOT/bonsai-chat.html:." \
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
  --hidden-import huggingface_hub \
  --collect-all huggingface_hub \
  "$ROOT/bonsai.py"

mkdir -p "$DMG_STAGE"
cp -R "$RELEASE_DIR/BonsaiChat.app" "$DMG_STAGE/"
xattr -cr "$DMG_STAGE/BonsaiChat.app" || true
hdiutil create \
  -volname BonsaiChat \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$RELEASE_DIR/BonsaiChat.dmg"

echo "Artifacts written to $RELEASE_DIR"
