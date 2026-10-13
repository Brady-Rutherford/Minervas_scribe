#!/usr/bin/env bash
# Package the Minerva's Scribe extension for Chrome Web Store upload.
# Usage: ./package.sh
# Output: minervas-scribe.zip in the project root

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_DIR="$SCRIPT_DIR/extension"
OUT="$SCRIPT_DIR/minervas-scribe.zip"

rm -f "$OUT"

cd "$EXT_DIR"
zip -r "$OUT" \
  manifest.json \
  background.js \
  content.js \
  popup.html \
  popup.js \
  popup.css \
  icons/

echo ""
echo "Packaged: $OUT"
echo "Size: $(du -h "$OUT" | cut -f1)"
echo ""
echo "Next: upload this ZIP at https://chrome.google.com/webstore/devconsole"
