#!/bin/sh
# The Hub side of run_hardware.sh, on the CM4 (started by it, not by hand), from the checkout it ships to
# <dir>/integration-esphome next to <dir>/integration-sdk; the venv is <dir>/venv.
#   hub.sh prepare: creates the venv with HUB_PYTHON (3.12+) if missing, then installs both checkouts and pytest into it
#   hub.sh test [pytest args]: routes AP_SUBNET via ROUTER_LINK_IP and runs the test against this host's BLE adapter.
#     stdin: line 1 is the hotspot password; EOF after it (the run was interrupted or lost its ssh session) stops the test.
# The route is removed on exit. Settings come from run_hardware.sh's environment (no defaults here).
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)
ROOT=$(dirname "$SRC")
VENV=$ROOT/venv
cd "$SRC"

if [ "${1:-}" = prepare ]; then
    : "${HUB_PYTHON:?}"
    "$HUB_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || {
        echo "$HUB_PYTHON is older than 3.12: set HUB_PYTHON" >&2
        exit 1
    }
    [ -x "$VENV/bin/python" ] || "$HUB_PYTHON" -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q -e "$ROOT/integration-sdk" -e "$SRC" pytest pytest-asyncio pytest-timeout
    exit
fi
[ "${1:-}" = test ] || { echo "Usage: $0 prepare | test [pytest args]" >&2; exit 2; }
shift
: "${AP_SUBNET:?}" "${ROUTER_LINK_IP:?}" "${ESPHOME_WIFI_SSID:?}" "${ESPHOME_BLE_ADAPTER:?}"

IFS= read -r ESPHOME_WIFI_PASSWORD
export ESPHOME_WIFI_PASSWORD
exec 3<&0  # the lifeline: an asynchronous list's stdin is /dev/null unless redirected

ROUTED='' TEST='' LIFELINE=''
# shellcheck disable=SC2329  # the EXIT trap
cleanup() {
    status=$?
    [ -z "$LIFELINE" ] || kill "$LIFELINE" 2>/dev/null || true
    [ -z "$TEST" ] || kill "$TEST" 2>/dev/null || true
    [ -z "$ROUTED" ] || sudo ip route del "$AP_SUBNET" via "$ROUTER_LINK_IP" ||
        echo "Remove the route by hand: sudo ip route del $AP_SUBNET via $ROUTER_LINK_IP" >&2
    exit "$status"  # not the trap's own
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
# `add`, not `replace`: a route already there (a hotspot of the CM4's own) is not ours to take over or remove
sudo ip route add "$AP_SUBNET" via "$ROUTER_LINK_IP"
ROUTED=1

"$VENV/bin/pytest" -p no:cacheprovider -v --include-hardware-iot -m hardware_iot_device tests/hardware "$@" &
TEST=$!
{ while IFS= read -r _; do :; done; kill "$TEST" 2>/dev/null; } <&3 &
LIFELINE=$!
STATUS=0
wait "$TEST" || STATUS=$?
TEST=''
exit "$STATUS"
