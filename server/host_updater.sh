#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# racecar-35 server updater — runs ON THE HOST, not in the container.
#
# WHY THIS EXISTS: the admin portal's "update server" button cannot rebuild the
# container from inside it (no docker socket, no git checkout, and
# `compose up --build` would kill the very process serving the request).
# Mounting docker.sock into the app would be root-equivalent on the host.
# So it's split: the portal WRITES a request, this script EXECUTES it.
#
#   portal:  POST /admin/update  ->  <data>/update_request.json
#   here:    git pull && docker compose -f docker-compose.prod.yml up -d --build
#            ...reporting progress back via <data>/update_status.json
#
# It reacts INSTANTLY — a systemd service blocks on inotify, so pressing the
# button fires the update immediately. No polling. A low-frequency safety timer
# is installed alongside purely to catch a request that somehow got missed
# (e.g. the watcher was down); disable it with --install --no-timer.
#
# SELF-LOCATING: it lives at <repo>/server/host_updater.sh and derives the repo,
# data and compose paths from its own location. Nothing to fill in. Run it from
# the repo — do NOT copy it to /usr/local/bin, that breaks self-location.
#
#   INSTALL (once, on the server box):
#       cd /docker/racecar.api.blueuc.com
#       sudo ./server/host_updater.sh --install
#
#   Modes:
#       --status      show detected paths, pending request, last result
#       --now         force an update right now, ignoring the request flag
#       --watch       run the instant watcher in the foreground (what the
#                     systemd service runs)
#       --install [--no-timer] | --uninstall
# ---------------------------------------------------------------------------
set -uo pipefail

# --- self-location (resolve symlinks so systemd/cron both work) -------------
SELF="$0"
while [ -L "$SELF" ]; do SELF="$(readlink -f "$SELF")"; done
case "$SELF" in /*) ;; *) SELF="$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF")" ;; esac
SERVER_DIR="$(cd "$(dirname "$SELF")" && pwd)"
REPO="${RACECAR_REPO:-$(cd "$SERVER_DIR/.." && pwd)}"
DATA="${RACECAR_DATA:-$SERVER_DIR/data}"
COMPOSE_FILE="$SERVER_DIR/docker-compose.prod.yml"

REQ="$DATA/update_request.json"
STAT="$DATA/update_status.json"
LOCK="$DATA/.update.lock"
SVC=/etc/systemd/system/racecar-updater.service
TIMER=/etc/systemd/system/racecar-updater-safety.timer
TIMERSVC=/etc/systemd/system/racecar-updater-safety.service

say() {
  mkdir -p "$DATA" 2>/dev/null
  printf '{"state":"%s","ts":%s,"detail":"%s"}\n' \
    "$1" "$(date +%s)" \
    "$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n' ' ' | cut -c1-400)" \
    > "$STAT.tmp" 2>/dev/null && mv "$STAT.tmp" "$STAT"
}

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"; fi
}

# --- the actual update. Safe to call concurrently (flock). ------------------
run_update() {
  mkdir -p "$DATA"
  exec 9>"$LOCK" || return 0
  flock -n 9 || { echo "another update is running"; return 0; }

  # Consume the request FIRST so a persistent failure cannot loop forever.
  [ -f "$REQ" ] && { mv -f "$REQ" "$DATA/.update_request.done" 2>/dev/null || rm -f "$REQ"; }

  cd "$REPO" || { say failed "repo $REPO missing"; return 1; }
  say pulling ""
  local out
  if ! out=$(git pull 2>&1); then say failed "git pull: $out"; return 1; fi
  say building "$(printf '%s' "$out" | tail -1)"

  cd "$SERVER_DIR" || { say failed "server dir missing"; return 1; }
  if ! out=$(compose -f "$COMPOSE_FILE" up -d --build 2>&1); then
    say failed "compose: $(printf '%s' "$out" | tail -3)"
    return 1
  fi
  say done "$(printf '%s' "$out" | tail -1)"

  flock -u 9
  return 0
}

case "${1:-}" in
  --status)
    echo "repo         : $REPO"
    echo "server dir   : $SERVER_DIR"
    echo "data dir     : $DATA"
    echo "compose file : $COMPOSE_FILE"
    w=$(systemctl is-active racecar-updater 2>/dev/null || true)
    t=$(systemctl is-active racecar-updater-safety.timer 2>/dev/null || true)
    echo "watcher      : ${w:-not installed}"
    echo "safety timer : ${t:-not installed}"
    command -v inotifywait >/dev/null 2>&1 \
      && echo "trigger      : inotify (instant)" \
      || echo "trigger      : 10 s sleep loop (install inotify-tools for instant)"
    echo "request      : $([ -f "$REQ" ] && echo PENDING || echo none)"
    echo -n "last status  : "; [ -f "$STAT" ] && cat "$STAT" || echo "(none)"
    exit 0 ;;

  --watch)
    mkdir -p "$DATA"
    echo "watching $DATA for update requests (repo: $REPO)"
    while true; do
      [ -f "$REQ" ] && run_update
      if command -v inotifywait >/dev/null 2>&1; then
        # Blocks until something appears in the data dir — instant reaction,
        # zero polling. The timeout is just a periodic sanity re-check.
        inotifywait -q -t 3600 -e create -e moved_to -e close_write "$DATA" >/dev/null 2>&1
      else
        sleep 10
      fi
    done ;;

  --install)
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
    [ -f "$COMPOSE_FILE" ] || { echo "no $COMPOSE_FILE — wrong repo?"; exit 1; }
    WANT_TIMER=1; [ "${2:-}" = "--no-timer" ] && WANT_TIMER=0
    command -v inotifywait >/dev/null 2>&1 || {
      echo "NOTE: inotify-tools not found — the watcher will fall back to a 10 s"
      echo "      loop. For instant response:  apt-get install -y inotify-tools"; }

    cat > "$SVC" <<EOF
[Unit]
Description=racecar-35 server updater (instant watcher for the admin update button)
After=docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=$SELF --watch
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now racecar-updater.service
    echo "watcher installed and running (instant, event-driven)."

    if [ "$WANT_TIMER" = "1" ]; then
      # Safety net ONLY: catches a request the watcher missed (e.g. it was down
      # while you pressed the button). Deliberately infrequent.
      cat > "$TIMERSVC" <<EOF
[Unit]
Description=racecar-35 updater safety sweep (catches a missed update request)

[Service]
Type=oneshot
ExecStart=$SELF
EOF
      cat > "$TIMER" <<EOF
[Unit]
Description=racecar-35 updater safety sweep

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30

[Install]
WantedBy=timers.target
EOF
      systemctl daemon-reload
      systemctl enable --now racecar-updater-safety.timer
      echo "safety sweep installed (every 5 min, only acts on a pending request)."
    else
      echo "safety sweep skipped (--no-timer)."
    fi
    echo "  script : $SELF"
    echo "  repo   : $REPO"
    echo "  data   : $DATA"
    exit 0 ;;

  --uninstall)
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
    systemctl disable --now racecar-updater.service 2>/dev/null
    systemctl disable --now racecar-updater-safety.timer 2>/dev/null
    rm -f "$SVC" "$TIMER" "$TIMERSVC"; systemctl daemon-reload
    echo "removed."; exit 0 ;;

  --now)  run_update; exit $? ;;
  "")     [ -f "$REQ" ] && { run_update; exit $?; }; exit 0 ;;
  *)      echo "unknown option: $1 (try --status, --install, --now, --watch)"; exit 1 ;;
esac
