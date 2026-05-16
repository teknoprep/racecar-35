#!/usr/bin/env bash
# scripts/release.sh — package a new CrowPanel firmware build into firmware/
#
# Usage:
#   ./scripts/release.sh <new-version>      # e.g. 0.1.1
#
# What it does:
#   1. Updates FIRMWARE_VERSION in crowpanel-arduino/RaceDash/RaceDash.ino
#   2. Builds the sketch with arduino-cli
#   3. Copies the resulting .bin to firmware/crowpanel-dash.bin
#   4. Computes sha256 + size, rewrites firmware/manifest.json
#   5. Leaves you with staged changes for `git commit && git push`
#
# After push, any dash on v0.1.0+ that taps "Check for updates" will see
# v<new-version> and offer to install.
#
# (Teensy section in manifest.json is updated separately once Teensy OTA
# lands in Phase 2b. For now the teensy entry is informational only.)

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <new-version>" >&2
  echo "       e.g. $0 0.1.1" >&2
  exit 1
fi

NEW_VER="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INO="$ROOT/crowpanel-arduino/RaceDash/RaceDash.ino"
FW_DIR="$ROOT/firmware"
FQBN='esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none'

if ! command -v arduino-cli >/dev/null; then
  echo "arduino-cli not in PATH; add ~/.local/bin or install it" >&2
  exit 1
fi

echo "== bump FIRMWARE_VERSION -> $NEW_VER"
sed -i "s/#define FIRMWARE_VERSION \"[^\"]*\"/#define FIRMWARE_VERSION \"$NEW_VER\"/" "$INO"
grep 'FIRMWARE_VERSION' "$INO" | head -1

echo "== build"
arduino-cli compile --fqbn "$FQBN" "$ROOT/crowpanel-arduino/RaceDash"

# arduino-cli caches builds at ~/.cache/arduino/sketches/<hash>/RaceDash.ino.bin
BIN=$(find "$HOME/.cache/arduino/sketches" -name 'RaceDash.ino.bin' -type f -printf '%T@ %p\n' \
       | sort -rn | head -1 | awk '{print $2}')
if [ -z "$BIN" ] || [ ! -f "$BIN" ]; then
  echo "could not locate built .bin under ~/.cache/arduino/sketches" >&2
  exit 1
fi
echo "== built: $BIN"

mkdir -p "$FW_DIR"
cp "$BIN" "$FW_DIR/crowpanel-dash.bin"
SHA=$(sha256sum "$FW_DIR/crowpanel-dash.bin" | awk '{print $1}')
SIZE=$(stat -c%s "$FW_DIR/crowpanel-dash.bin")
echo "== sha256=$SHA size=$SIZE"

cat > "$FW_DIR/manifest.json" <<EOF
{
  "crowpanel": {
    "version": "$NEW_VER",
    "url":     "https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/crowpanel-dash.bin",
    "sha256":  "$SHA",
    "size":    $SIZE
  },
  "teensy": {
    "version": "0.1.0",
    "url":     "https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/teensy41-dash.hex",
    "sha256":  "(not-yet-built)",
    "note":    "Teensy OTA pending Phase 2b (FlasherX + UART transfer)"
  }
}
EOF
# Defensively touch both artifacts so git's stat-cache notices content changes
# even when mtime + size happen to match the previous version (we hit this in
# v0.1.1 and v0.1.2 — manifest content changed but mtime didn't, so the file
# silently dropped out of `git status`).
touch "$FW_DIR/crowpanel-dash.bin" "$FW_DIR/manifest.json"
echo "== firmware/manifest.json updated"

cat <<EOF

NEXT STEPS:
  git add crowpanel-arduino/RaceDash/RaceDash.ino firmware/
  git commit -m "Release v$NEW_VER"
  git push

Any dash running an older OTA-capable firmware will then see v$NEW_VER
on its next "Check for updates" tap.
EOF
