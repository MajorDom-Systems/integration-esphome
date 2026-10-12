#!/bin/sh
# Lab run of test_improv_hardware.py: prepared, not yet run on the rig.
#
# Rig: lab-pi5 is the Hub (inside the cage: BLE on hci0, LAN on eth0, its own Wi-Fi is disabled). The ESP32-C3 DUT,
# flashed with esp32c3_improv.yaml, sits in the cage on lab-pi5's USB (power and console). AP_HOST is a lab machine
# whose Wi-Fi reaches the cage; it runs hostapd bridged into the LAN (AP_BRIDGE enslaves its LAN port, set up once),
# so the DUT takes a LAN address and its mDNS reaches lab-pi5.
#
# Usage, on lab-pi5 from the repo checkout (rsynced from the Mac, never cloned):
#   AP_HOST=admin@<ap-host> tests/hardware/run_hardware.sh [pytest args]
# Optional: AP_IFACE (wlan0), AP_BRIDGE (br0), AP_SSID (majordom-esphome-hw), AP_CHANNEL (6: the C3 is 2.4 GHz only),
#   AUTHORIZER=1 when the DUT was built with `-s authorizer boot_button` (press BOOT when asked).
# A DUT left provisioned by an aborted run: reflash it (`esphome run ... --device <by-id>` after `esptool erase_flash`).
set -eu

: "${AP_HOST:?set AP_HOST to the ssh target that runs the access point}"
AP_IFACE=${AP_IFACE:-wlan0}
AP_BRIDGE=${AP_BRIDGE:-br0}
AP_SSID=${AP_SSID:-majordom-esphome-hw}
AP_CHANNEL=${AP_CHANNEL:-6}
AP_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')  # per run, never printed
REMOTE_CONF=/tmp/esphome-hw-hostapd.conf
REMOTE_PID=/tmp/esphome-hw-hostapd.pid

# 1. The CI runner owns the rig while it holds the Consul lock
if curl -s localhost:8500/v1/kv/github-runner/iot-lock | grep -q '"Session"'; then
    echo "The GitHub runner holds github-runner/iot-lock: not touching the rig" >&2
    exit 1
fi

# 2. One cage user at a time
exec 9>"$HOME/.cage-hw.lock"
flock -n 9 || { echo "Another cage session holds ~/.cage-hw.lock" >&2; exit 1; }

# 3. The access point, torn down whatever happens
stop_ap() {
    ssh "$AP_HOST" "sudo kill \$(cat $REMOTE_PID) 2>/dev/null; rm -f $REMOTE_CONF $REMOTE_PID; \
        sudo nmcli device set $AP_IFACE managed yes 2>/dev/null || true"
}
trap stop_ap EXIT INT TERM
ssh "$AP_HOST" "umask 077 && cat > $REMOTE_CONF" <<CONF
interface=$AP_IFACE
bridge=$AP_BRIDGE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=$AP_CHANNEL
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$AP_PASSWORD
CONF
ssh "$AP_HOST" "sudo nmcli device set $AP_IFACE managed no 2>/dev/null || true; \
    sudo hostapd -B -P $REMOTE_PID $REMOTE_CONF"

# 4. The test: the Hub side runs here, against the real BLE and mDNS
[ "${AUTHORIZER:-0}" = 1 ] && echo "Press BOOT on the ESP32-C3 when the Hub asks for it (within 60 s)"
ESPHOME_HW_SSID=$AP_SSID ESPHOME_HW_PASSWORD=$AP_PASSWORD \
    "$HOME/.local/bin/poetry" run pytest -p no:cacheprovider -v tests/hardware "$@"
