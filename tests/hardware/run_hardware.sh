#!/bin/sh
# Lab run of test_improv_hardware.py (passed on the rig 2026-10-13).
#
# Rig: lab-pi5 is the Hub (inside the cage: BLE on hci0, its own Wi-Fi is disabled). The ESP32-C3 DUT, flashed with
# esp32c3_improv.yaml, sits in the cage on lab-pi5's USB (power and console). AP_HOST is the CM4 bench board, cabled
# to lab-pi5 (lab-pi5 AP_LINK <-> CM4 AP_GATEWAY): it runs a temporary NetworkManager hotspot, so the DUT lands on the
# hotspot's NAT subnet (AP_SUBNET). lab-pi5 routes that subnet through the CM4, and the CM4's avahi reflector carries
# the DUT's mDNS over the cable. Everything set up here is reverted on exit.
#
# Usage, on lab-pi5 from the repo checkout (rsynced from the Mac, never cloned), logged in with `ssh -A` (lab-pi5 has
# no key of its own for the CM4):
#   AP_HOST=admin@10.99.0.2 tests/hardware/run_hardware.sh [pytest args]
# Optional: AP_SSH_OPTS (e.g. `-o UserKnownHostsFile=<file with the CM4 host key>`), AP_IFACE (wlan0), AP_LINK (eth1),
#   AP_GATEWAY (10.99.0.2), AP_SUBNET (10.42.0.0/24: NetworkManager's shared default), AP_SSID (MajorDom-Improv-Test),
#   AP_CHANNEL (6: the C3 is 2.4 GHz only), PYTEST (poetry run pytest),
#   AUTHORIZER=1 when the DUT was built with `-s authorizer boot_button` (press BOOT when asked).
# A DUT left provisioned by an aborted run: reflash it (`esptool erase-flash`, then write the factory image).
set -eu

: "${AP_HOST:?set AP_HOST to the ssh target that runs the access point}"
AP_SSH_OPTS=${AP_SSH_OPTS:-}
AP_IFACE=${AP_IFACE:-wlan0}
AP_LINK=${AP_LINK:-eth1}
AP_GATEWAY=${AP_GATEWAY:-10.99.0.2}
AP_SUBNET=${AP_SUBNET:-10.42.0.0/24}
AP_SSID=${AP_SSID:-MajorDom-Improv-Test}
AP_CHANNEL=${AP_CHANNEL:-6}
PYTEST=${PYTEST:-"$HOME/.local/bin/poetry run pytest"}
AP_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')  # per run, never printed
AP_CON=esphome-hw-ap
HUB_SUBNET=$(ip -4 -o addr show "$AP_LINK" | awk '{print $4}')  # the cable's subnet, forwarded into the hotspot
# The AP host's state to restore: kept there, in root-only tmpfs
STATE=/run/esphome-hw-ap
NODNS=/etc/NetworkManager/dnsmasq-shared.d/esphome-hw-nodns.conf
AVAHI=/etc/avahi/avahi-daemon.conf

ap() {
    # shellcheck disable=SC2086,SC2029  # AP_SSH_OPTS is a list of options; commands expand here on purpose
    ssh $AP_SSH_OPTS "$AP_HOST" "$@"
}

# 1. The CI runner owns the rig while it holds the Consul lock
if curl -s localhost:8500/v1/kv/github-runner/iot-lock | grep -q '"Session"'; then
    echo "The GitHub runner holds github-runner/iot-lock: not touching the rig" >&2
    exit 1
fi

# 2. One cage user at a time
exec 9>"$HOME/.cage-hw.lock"
flock -n 9 || { echo "Another cage session holds ~/.cage-hw.lock" >&2; exit 1; }

# 3. The access point, the route and the mDNS reflector, all reverted whatever happens
stop_ap() {
    sudo ip route del "$AP_SUBNET" via "$AP_GATEWAY" dev "$AP_LINK" 2>/dev/null || true
    # Deleting the connection also drops NetworkManager's nm-shared table, with the forward rule inserted below
    ap "sudo nmcli connection delete $AP_CON >/dev/null 2>&1; sudo rm -f $NODNS; \
        if sudo test -f $STATE/avahi-daemon.conf; then sudo cp $STATE/avahi-daemon.conf $AVAHI; \
            sudo systemctl restart avahi-daemon; fi; \
        if sudo test -f $STATE/radio-off; then sudo nmcli radio wifi off; sudo rfkill block wifi; fi; \
        sudo rm -rf $STATE" || echo "Revert the AP on $AP_HOST by hand: connection $AP_CON, $NODNS, $AVAHI" >&2
}
trap stop_ap EXIT
trap 'exit 130' INT TERM
# NetworkManager's dnsmasq would take :53, which bind9 holds on the CM4: DHCP only. The password goes over stdin.
printf '%s\n' "$AP_PASSWORD" | ap "read -r PW && set -e && sudo mkdir -m 700 $STATE && \
    if [ \"\$(nmcli radio wifi)\" != enabled ]; then sudo touch $STATE/radio-off; fi && \
    sudo cp -p $AVAHI $STATE/avahi-daemon.conf && \
    sudo rfkill unblock wifi && sudo nmcli radio wifi on && sleep 3 && \
    echo port=0 | sudo tee $NODNS >/dev/null && \
    sudo nmcli dev wifi hotspot ifname $AP_IFACE con-name $AP_CON ssid $AP_SSID password \"\$PW\" band bg \
        channel $AP_CHANNEL >/dev/null && \
    sudo nft insert rule ip nm-shared-$AP_IFACE filter_forward ip saddr $HUB_SUBNET oifname $AP_IFACE accept && \
    sudo sed -i 's/^#*enable-reflector=.*/enable-reflector=yes/' $AVAHI && sudo systemctl restart avahi-daemon"
sudo ip route replace "$AP_SUBNET" via "$AP_GATEWAY" dev "$AP_LINK"

# 4. The test: the Hub side runs here, against the real BLE and mDNS
[ "${AUTHORIZER:-0}" = 1 ] && echo "Press BOOT on the ESP32-C3 when the Hub asks for it (within 60 s)"
# shellcheck disable=SC2086  # PYTEST is a command line
ESPHOME_HW_SSID=$AP_SSID ESPHOME_HW_PASSWORD=$AP_PASSWORD $PYTEST -p no:cacheprovider -v tests/hardware "$@"
