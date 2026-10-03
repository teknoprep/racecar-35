#!/usr/bin/env bash
# RC35 bench CAN console - start / stop / status.
#
#   ./tools/canbench.sh start [port] [hz]     start the GUI + broadcaster (defaults
#                                             /dev/ttyACM0 and 100 Hz) and open DASH VIEW
#                                             from the button in the window
#   ./tools/canbench.sh stop                  stop both and free the adapter
#   ./tools/canbench.sh status                what is running right now
#
# The GUI spawns the broadcaster itself, so there is only ever one process holding the
# CANable: two openers means "device busy" and looks exactly like a dead adapter.
#
# Logs: /tmp/can_gui.log  (the broadcaster's output is also in the GUI's log pane)
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${2:-/dev/ttyACM0}"
HZ="${3:-100}"

# Anchored so these can never match THIS script's own command line (that mistake killed a
# shell twice during development and left a stale broadcaster holding the port).
GUI_PAT='^python3 tools/can_gui\.py'
SIM_PAT='^/usr/bin/python3 -u .*tools/can_sim\.py'

status() {
    echo "GUI app:"
    pgrep -af "$GUI_PAT" | sed 's/^/  /' || echo "  not running"
    echo "broadcaster (holds the CANable):"
    pgrep -af "$SIM_PAT" | sed 's/^/  /' || echo "  not running"
}

case "${1:-status}" in
    start)
        pkill -f "$GUI_PAT" 2>/dev/null || true
        pkill -f "$SIM_PAT" 2>/dev/null || true
        sleep 1
        cd "$HERE"
        DISPLAY="${DISPLAY:-:0}" setsid nohup python3 tools/can_gui.py \
            --port "$PORT" --hz "$HZ" --autostart > /tmp/can_gui.log 2>&1 < /dev/null &
        disown
        sleep 3
        echo "started  (port $PORT, ${HZ} Hz)"
        status
        ;;
    stop)
        pkill -f "$GUI_PAT" 2>/dev/null || true
        pkill -f "$SIM_PAT" 2>/dev/null || true
        sleep 1
        echo "stopped"
        status
        ;;
    *)
        status
        ;;
esac
