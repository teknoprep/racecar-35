#!/usr/bin/env bash
# Publish the four OTA artifacts + a server-pointed manifest to the racecar
# cloud server's /firmware store. Run AFTER building (see ../BUILD.md), from the
# repo root's firmware/ dir being present.
#
# Usage:
#   RACECAR_API_KEY=... ./server/publish_firmware.sh <version> [base_url]
#
#   <version>   e.g. 0.1.73  (written into every manifest entry)
#   [base_url]  default https://racecar.api.blueuc.com
#
# The manifest's artifact URLs are set to <base_url>/firmware/<file>, and the
# server serves manifest.json no-store so the dash sees a new version instantly
# (no GitHub-CDN 5-min lag). Requires: curl, sha256sum, stat.
set -euo pipefail

VER="${1:?usage: publish_firmware.sh <version> [base_url]}"
BASE="${2:-https://racecar.api.blueuc.com}"
KEY="${RACECAR_API_KEY:?set RACECAR_API_KEY to match the server RACECAR_API_KEY env}"

# repo-root/firmware regardless of where we're called from
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="$ROOT/firmware"

declare -A BOARD=(
  [teensy41-dash.hex]=teensy
  [crowpanel7-dash.bin]=crowpanel7
  [crowpanel5-dash.bin]=crowpanel5
  [crowpanel5adv-dash.bin]=crowpanel5adv
)

# 1. Upload each binary artifact.
for f in teensy41-dash.hex crowpanel7-dash.bin crowpanel5-dash.bin crowpanel5adv-dash.bin; do
  [ -f "$FW/$f" ] || { echo "missing $FW/$f (build first)"; exit 1; }
  echo "-> uploading $f ($(stat -c%s "$FW/$f") bytes)"
  curl -fsS -X POST "$BASE/firmware/upload?name=$f" \
       -H "X-API-Key: $KEY" \
       --data-binary "@$FW/$f" | sed 's/^/   /'
  echo
done

# 2. Build a server-pointed manifest and upload it LAST (so it only ever points
#    at artifacts already present on the server).
emit_entry() { # <key> <board> <file>  -> JSON object
  local key="$1" board="$2" file="$3"
  local sha size
  sha="$(sha256sum "$FW/$file" | cut -d' ' -f1)"
  size="$(stat -c%s "$FW/$file")"
  printf '  "%s": {\n    "board":   "%s",\n    "version": "%s",\n    "url":     "%s/firmware/%s",\n    "sha256":  "%s",\n    "size":    %s\n  }' \
    "$key" "$board" "$VER" "$BASE" "$file" "$sha" "$size"
}

MAN="$(mktemp)"
{
  echo "{"
  emit_entry teensy        teensy        teensy41-dash.hex;      echo ","
  emit_entry crowpanel7    crowpanel7    crowpanel7-dash.bin;    echo ","
  emit_entry crowpanel5    crowpanel5    crowpanel5-dash.bin;    echo ","
  emit_entry crowpanel5adv crowpanel5adv crowpanel5adv-dash.bin; echo ","
  # legacy alias: key "crowpanel" but board mirrors crowpanel7
  emit_entry crowpanel     crowpanel7    crowpanel7-dash.bin
  echo ""
  echo "}"
} > "$MAN"

echo "-> uploading manifest.json (version $VER, base $BASE)"
curl -fsS -X POST "$BASE/firmware/upload?name=manifest.json" \
     -H "X-API-Key: $KEY" \
     --data-binary "@$MAN" | sed 's/^/   /'
echo
rm -f "$MAN"

echo "-> verify:"
curl -fsS "$BASE/firmware/manifest.json" | grep -E '"version"|"url"' | sed 's/^/   /'
echo "done. Dash OTA_MANIFEST_URL should be $BASE/firmware/manifest.json"
