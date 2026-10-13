#!/bin/sh
# Lab run of test_improv_hardware.py: the ESP32-C3 DUT (flashed with esp32c3_improv.yaml) is found over BLE, handed
# a Wi-Fi network over Improv, found again over mDNS and paired with a key of its own.
#
# WARNING: an onboard Raspberry Pi Wi-Fi/BT combo chip hosting an AP stops BLE scanning (observed: 0 devices), so the
# hotspot and the Hub's BLE must run on different hosts (or at least different radios).
#
# Rig, two hosts on a cable (lab-pi5 HUB_LINK 10.99.0.1 <-> CM4 10.99.0.2):
#   lab-pi5, the router (tests/hardware/router.sh): holds the cage (Consul iot-lock check, ~/.cage-hw.lock), keeps
#     the DUT on its in-cage USB (power, flashing, console), runs a NetworkManager hotspot on its own Wi-Fi with a
#     random per-run password, forwards the cable into it and reflects mDNS between them (avahi).
#   CM4, the Hub (tests/hardware/hub.sh): runs pytest with its own BLE adapter (Improv discovery, provisioning and the
#     zero-PSK key), reaching the DUT through a temporary route to AP_SUBNET via lab-pi5.
# This script runs on the Mac and drives both over ssh, the CM4 through lab-pi5 as a ProxyJump: authentication stays
# on the Mac, no agent forwarding and no key on lab-pi5. It ships both checkouts' git-tracked (and untracked,
# unignored) files to the CM4, prepares the venv there, starts the router side, then the test. Each side reverts
# itself when its stdin closes, so an interrupted run or a dropped ssh session leaves both hosts as found.
#
# Usage, from the repo checkout on the Mac (SDK_SRC: the SDK checkout to test against):
#   SDK_SRC=../integration-sdk tests/hardware/run_hardware.sh [pytest args]
# Or on lab-pi5 itself (ROUTER_SSH=local, from a checkout rsynced there): the CM4 is then reached directly, which needs
#   a key for it there or an agent forwarded with `ssh -A` (avoid: prefer the Mac).
# Optional: ROUTER_SSH (admin@lab-pi5, or local), ROUTER_SSH_OPTS, ROUTER_DIR (esphome-hw, under the router user's
#   home), HUB_SSH (admin@10.99.0.2), HUB_SSH_OPTS (-J $ROUTER_SSH; none when ROUTER_SSH=local), HUB_DIR (esphome-hw,
#   under the Hub user's home), HUB_PYTHON (python3, 3.12+), AP_IFACE (wlan0), AP_SSID (MajorDom-Improv-Test),
#   AP_CHANNEL (6: the C3 is 2.4 GHz only), AP_SUBNET (10.42.0.0/24: NetworkManager's shared default), HUB_LINK (eth1),
#   ROUTER_LINK_IP (10.99.0.1), ESPHOME_DUT_SERIAL (the C3's /dev/serial/by-id path on lab-pi5), AUTHORIZER=1 when the
#   DUT was built with `-s authorizer boot_button` (press BOOT when asked), and the test's ESPHOME_BLE_ADAPTER (hci0,
#   on the CM4), ESPHOME_DISCOVERY_TIMEOUT, ESPHOME_TEST_TIMEOUT.
# A DUT left provisioned by an aborted run: reflash it (`esptool erase-flash`, then write the factory image).
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
SDK_SRC=${SDK_SRC:-$REPO/../integration-sdk}
ROUTER_SSH=${ROUTER_SSH:-admin@lab-pi5}
ROUTER_SSH_OPTS=${ROUTER_SSH_OPTS:-}
ROUTER_DIR=${ROUTER_DIR:-esphome-hw}
HUB_SSH=${HUB_SSH:-admin@10.99.0.2}
if [ "$ROUTER_SSH" = local ]; then HUB_SSH_OPTS=${HUB_SSH_OPTS:-}; else HUB_SSH_OPTS=${HUB_SSH_OPTS:--J $ROUTER_SSH}; fi
HUB_DIR=${HUB_DIR:-esphome-hw}
HUB_PYTHON=${HUB_PYTHON:-python3}
AP_IFACE=${AP_IFACE:-wlan0}
AP_SSID=${AP_SSID:-MajorDom-Improv-Test}
AP_CHANNEL=${AP_CHANNEL:-6}
AP_SUBNET=${AP_SUBNET:-10.42.0.0/24}
HUB_LINK=${HUB_LINK:-eth1}
ROUTER_LINK_IP=${ROUTER_LINK_IP:-10.99.0.1}
ESPHOME_DUT_SERIAL=${ESPHOME_DUT_SERIAL:-/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_7C:4F:AD:D1:7B:00-if00}
ESPHOME_BLE_ADAPTER=${ESPHOME_BLE_ADAPTER:-hci0}
AP_PASSWORD=$(LC_ALL=C tr -dc A-Za-z0-9 </dev/urandom | head -c 24)  # per run, only ever on the sides' stdin

# One shell word for the remote shell
q() { printf "'%s'" "$(printf %s "$1" | sed "s/'/'\\\\''/g")"; }

on_router() {
    if [ "$ROUTER_SSH" = local ]; then
        sh -c "$1"
    else
        # shellcheck disable=SC2086,SC2029  # the options are a list; the command is built for the remote shell
        ssh $ROUTER_SSH_OPTS "$ROUTER_SSH" "$1"
    fi
}

on_hub() {
    # shellcheck disable=SC2086,SC2029  # the options are a list; the command is built for the remote shell
    ssh $HUB_SSH_OPTS "$HUB_SSH" "$1"
}

ship() {  # <checkout> <name>: its files, as in the working tree, to $HUB_DIR/<name> on the Hub
    git -C "$1" ls-files -z --cached --others --exclude-standard |
        COPYFILE_DISABLE=1 tar -C "$1" --no-xattrs --null -T - -cf - |
        on_hub "rm -rf $(q "$HUB_DIR/$2") && mkdir -p $(q "$HUB_DIR/$2") && \
            tar -xf - --warning=no-unknown-keyword -C $(q "$HUB_DIR/$2")"
}

# 1. The Hub's code and venv (nothing on the rig is touched yet)
[ -f "$SDK_SRC/majordom_integration_sdk/__init__.py" ] || { echo "No SDK checkout at $SDK_SRC: set SDK_SRC" >&2; exit 1; }
ship "$REPO" integration-esphome
ship "$SDK_SRC" integration-sdk
on_hub "HUB_PYTHON=$(q "$HUB_PYTHON") sh $(q "$HUB_DIR/integration-esphome/tests/hardware/hub.sh") prepare"

# 2. The router side: the cage and the hotspot, up until fd 3 closes
ROUTER_ENV="AP_IFACE=$(q "$AP_IFACE") AP_SSID=$(q "$AP_SSID") AP_CHANNEL=$(q "$AP_CHANNEL") \
    HUB_LINK=$(q "$HUB_LINK") ESPHOME_DUT_SERIAL=$(q "$ESPHOME_DUT_SERIAL")"
if [ "$ROUTER_SSH" = local ]; then
    ROUTER_SCRIPT=$REPO/tests/hardware/router.sh
else
    ROUTER_SCRIPT=$ROUTER_DIR/router.sh
    on_router "mkdir -p $(q "$ROUTER_DIR") && cat >$(q "$ROUTER_SCRIPT")" <"$REPO/tests/hardware/router.sh"
fi
FIFOS=$(mktemp -d)
mkfifo "$FIFOS/router-in" "$FIFOS/router-out" "$FIFOS/hub-in"
# shellcheck disable=SC2329  # the EXIT trap
finish() {
    status=$?
    exec 3>&- 6>&-  # EOF: each side reverts itself
    wait
    rm -rf "$FIFOS"
    exit "$status"  # not the trap's own
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
trap '' PIPE  # a side gone early fails the write instead of killing this script past its EXIT trap
on_router "env $ROUTER_ENV sh $(q "$ROUTER_SCRIPT")" <"$FIFOS/router-in" >"$FIFOS/router-out" &
exec 3>"$FIFOS/router-in" 4<"$FIFOS/router-out"
printf '%s\n' "$AP_PASSWORD" >&3
IFS= read -r READY <&4 || READY=''
[ "$READY" = ready ] || { echo "The router side did not come up" >&2; exit 1; }
cat <&4 >&2 3>&- &  # without fd 3, or the router side never sees its EOF

# 3. The test, on the Hub, against the real BLE and mDNS
[ "${AUTHORIZER:-0}" = 1 ] && echo "Press BOOT on the ESP32-C3 when the Hub asks for it (within 60 s)"
HUB_ENV="AP_SUBNET=$(q "$AP_SUBNET") ROUTER_LINK_IP=$(q "$ROUTER_LINK_IP") ESPHOME_WIFI_SSID=$(q "$AP_SSID") \
    ESPHOME_BLE_ADAPTER=$(q "$ESPHOME_BLE_ADAPTER")"
[ -z "${ESPHOME_DISCOVERY_TIMEOUT:-}" ] ||
    HUB_ENV="$HUB_ENV ESPHOME_DISCOVERY_TIMEOUT=$(q "$ESPHOME_DISCOVERY_TIMEOUT")"
[ -z "${ESPHOME_TEST_TIMEOUT:-}" ] || HUB_ENV="$HUB_ENV ESPHOME_TEST_TIMEOUT=$(q "$ESPHOME_TEST_TIMEOUT")"
ARGS=''
for arg in "$@"; do ARGS="$ARGS $(q "$arg")"; done
on_hub "env $HUB_ENV sh $(q "$HUB_DIR/integration-esphome/tests/hardware/hub.sh") test$ARGS" <"$FIFOS/hub-in" 3>&- 4<&- &
HUB=$!
exec 6>"$FIFOS/hub-in"
printf '%s\n' "$AP_PASSWORD" >&6
STATUS=0
wait "$HUB" || STATUS=$?
exit "$STATUS"
