#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# racecar-35 server updater — runs ON THE HOST, not in the container.
#
# The admin portal's "update server" button cannot rebuild the container from
# inside it (no docker socket, no git checkout, and the rebuild would kill the
# process serving the request). Instead the app writes
#   <data>/update_request.json
# and this script — polled by cron or systemd on the host — performs the real
# work and reports back via
#   <data>/update_status.json
#
# Install (systemd timer, every 30 s):
#   sudo cp server/host_updater.sh /usr/local/bin/racecar-updater
#   sudo chmod +x /usr/local/bin/racecar-updater
#   # /etc/systemd/system/racecar-updater.service
#   #   [Service]
#   #   Type=oneshot
#   #   Environment=RACECAR_REPO=/path/to/racecar-35
#   #   Environment=RACECAR_DATA=/path/to/racecar-35/server/data
#   #   ExecStart=/usr/local/bin/racecar-updater
#   # /etc/systemd/system/racecar-updater.timer
#   #   [Timer]
#   #   OnUnitActiveSec=30
#   #   AccuracySec=5
#   #   [Install]
#   #   WantedBy=timers.target
#   sudo systemctl enable --now racecar-updater.timer
#
# Or simply, via cron:
#   * * * * * RACECAR_REPO=/path/to/racecar-35 RACECAR_DATA=/path/to/racecar-35/server/data /usr/local/bin/racecar-updater
# ---------------------------------------------------------------------------
set -uo pipefail

REPO="${RACECAR_REPO:?set RACECAR_REPO to the racecar-35 checkout}"
DATA="${RACECAR_DATA:-$REPO/server/data}"
REQ="$DATA/update_request.json"
STAT="$DATA/update_status.json"
LOCK="$DATA/.update.lock"

[ -f "$REQ" ] || exit 0                       # nothing requested

exec 9>"$LOCK" || exit 0
flock -n 9 || exit 0                          # an update is already running

say() {
  printf '{"state":%s,"ts":%s,"detail":%s}\n' \
    "\"$1\"" "$(date +%s)" "\"$(printf '%s' "${2:-}" | tr -d '"' | tr '\n' ' ' | cut -c1-400)\"" \
    > "$STAT.tmp" && mv "$STAT.tmp" "$STAT"
}

# Consume the request FIRST so a failure can't cause an endless rebuild loop.
mv -f "$REQ" "$DATA/.update_request.done" 2>/dev/null || rm -f "$REQ"

cd "$REPO" || { say failed "repo $REPO missing"; exit 1; }

say pulling ""
if ! out=$(git pull 2>&1); then say failed "git pull: $out"; exit 1; fi
say building "$(printf '%s' "$out" | tail -1)"

cd "$REPO/server" || { say failed "server dir missing"; exit 1; }
if ! out=$(docker compose -f docker-compose.prod.yml up -d --build 2>&1); then
  say failed "compose: $(printf '%s' "$out" | tail -3)"
  exit 1
fi

say done "$(printf '%s' "$out" | tail -1)"
