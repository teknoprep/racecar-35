#!/usr/bin/env bash
# Publish the THREE OTA artifacts (teensy + the two Advance panels) + a
# server-pointed manifest to the racecar cloud server's /firmware store.
# The Basic panels (crowpanel7 / crowpanel5) were RETIRED (scrapped hardware)
# after v0.1.146 and are no longer built or published. Run AFTER building (see ../BUILD.md), from the
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

# Artifact dir. Default was the repo's firmware/ dir, but that is now FROZEN
# as the GitHub legacy-OTA bridge (teensy 0.1.99 / dash 0.1.98 — see CLAUDE.md)
# and must never be overwritten by a release. Stage fresh builds elsewhere and
# point RACECAR_FW_DIR at them, e.g.:
#   mkdir -p /tmp/fwstage && cp .pio/build/teensy41/firmware.hex /tmp/fwstage/teensy41-dash.hex && ...
#   RACECAR_FW_DIR=/tmp/fwstage RACECAR_API_KEY=... ./server/publish_firmware.sh <ver>
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="${RACECAR_FW_DIR:-$ROOT/firmware}"
if [ -z "${RACECAR_FW_DIR:-}" ]; then
  echo "WARNING: RACECAR_FW_DIR not set - using $FW, which is the FROZEN GitHub"
  echo "         bridge (teensy 0.1.99 / dash 0.1.98). If you are publishing a"
  echo "         NEW release you almost certainly want a staging dir instead."
fi

ARTIFACTS=(teensy41-dash.hex crowpanel5adv-dash.bin crowpanel7adv-dash.bin)

# 1. Upload each binary artifact.
for f in "${ARTIFACTS[@]}"; do
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
  emit_entry crowpanel5adv crowpanel5adv crowpanel5adv-dash.bin; echo ","
  emit_entry crowpanel7adv crowpanel7adv crowpanel7adv-dash.bin
  # (crowpanel7 / crowpanel5 / legacy "crowpanel" alias RETIRED after 0.1.146.)
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
