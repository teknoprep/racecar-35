#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# racecar-35 server updater — runs ON THE HOST, not in the container.
#
# The admin portal's "update server" button cannot rebuild the container from
# inside it (no docker socket, no git checkout, and the rebuild would kill the
# process serving the request). Instead the app writes
#   <data>/update_request.json
# and this script performs the real work, reporting back via
#   <data>/update_status.json
#
# It is SELF-LOCATING: it lives at <repo>/server/host_updater.sh, so it works
# out the repo and data paths on its own. Nothing to fill in.
#
#   INSTALL (once, from the repo on the server box):
#       cd /wherever/racecar-35
#       sudo ./server/host_updater.sh --install
#
#   That's it. It installs a systemd timer that polls every 30 s, pointed at
#   this script in place. Run it again after moving the repo.
#
#   Other modes:
#       ./server/host_updater.sh            # check once (what the timer runs)
#       ./server/host_updater.sh --now      # force an update, ignoring the flag
#       ./server/host_updater.sh --status    # show detected paths + last status
#       sudo ./server/host_updater.sh --uninstall
#
#   No systemd? One cron line instead (paths resolve themselves):
#       * * * * * /wherever/racecar-35/server/host_updater.sh
# ---------------------------------------------------------------------------
set -uo pipefail

# --- self-location (resolve symlinks so `systemctl` and cron both work) -----
SELF="$0"
while [ -L "$SELF" ]; do SELF="$(readlink -f "$SELF")"; done
SERVER_DIR="$(cd "$(dirname "$SELF")" && pwd)"
REPO="${RACECAR_REPO:-$(cd "$SERVER_DIR/.." && pwd)}"
DATA="${RACECAR_DATA:-$SERVER_DIR/data}"
COMPOSE_FILE="$SERVER_DIR/docker-compose.prod.yml"

REQ="$DATA/update_request.json"
STAT="$DATA/update_status.json"
LOCK="$DATA/.update.lock"
UNIT=/etc/systemd/system/racecar-updater.service
TIMER=/etc/systemd/system/racecar-updater.timer

say() {
  mkdir -p "$DATA" 2>/dev/null
  printf '{"state":"%s","ts":%s,"detail":"%s"}\n' \
    "$1" "$(date +%s)" \
    "$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n' ' ' | cut -c1-400)" \
    > "$STAT.tmp" 2>/dev/null && mv "$STAT.tmp" "$STAT"
}

# --- docker compose v2 or legacy v1 ----------------------------------------
compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"; fi
}

case "${1:-}" in
  --status)
    echo "repo         : $REPO"
    echo "server dir   : $SERVER_DIR"
    echo "data dir     : $DATA"
    echo "compose file : $COMPOSE_FILE"
    echo "request      : $([ -f "$REQ" ] && echo PENDING || echo none)"
    echo -n "last status  : "; [ -f "$STAT" ] && cat "$STAT" || echo "(none)"
    exit 0 ;;

  --install)
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
    [ -f "$COMPOSE_FILE" ] || { echo "no $COMPOSE_FILE — is this the right repo?"; exit 1; }
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=racecar-35 server updater (serves the admin portal's update button)
After=docker.service

[Service]
Type=oneshot
ExecStart=$SELF
UNITEOF
    cat > "$TIMER" <<TIMEREOF
[Unit]
Description=Poll for racecar-35 server update requests

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
TIMEREOF
    systemctl daemon-reload
    systemctl enable --now racecar-updater.timer
    echo "installed. polling every 30 s."
    echo "  script : $SELF"
    echo "  repo   : $REPO"
    echo "  data   : $DATA"
    systemctl list-timers racecar-updater.timer --no-pager 2>/dev/null | tail -2
    exit 0 ;;

  --uninstall)
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
    systemctl disable --now racecar-updater.timer 2>/dev/null
    rm -f "$UNIT" "$TIMER"; systemctl daemon-reload
    echo "removed."; exit 0 ;;

  --now)  FORCE=1 ;;
  "")     FORCE=0 ;;
  *)      echo "unknown option: $1 (try --status, --install, --now)"; exit 1 ;;
esac

# --- normal path: only act when the portal asked (or --now) ----------------
if [ "$FORCE" != "1" ] && [ ! -f "$REQ" ]; then exit 0; fi

mkdir -p "$DATA"
exec 9>"$LOCK" || exit 0
flock -n 9 || exit 0                     # an update is already running

# Consume the request FIRST so a persistent failure can't loop forever.
[ -f "$REQ" ] && { mv -f "$REQ" "$DATA/.update_request.done" 2>/dev/null || rm -f "$REQ"; }

cd "$REPO" || { say failed "repo $REPO missing"; exit 1; }

say pulling ""
if ! out=$(git pull 2>&1); then say failed "git pull: $out"; exit 1; fi
say building "$(printf '%s' "$out" | tail -1)"

cd "$SERVER_DIR" || { say failed "server dir missing"; exit 1; }
if ! out=$(compose -f "$COMPOSE_FILE" up -d --build 2>&1); then
  say failed "compose: $(printf '%s' "$out" | tail -3)"
  exit 1
fi

say done "$(printf '%s' "$out" | tail -1)"
