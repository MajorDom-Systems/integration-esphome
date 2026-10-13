#!/bin/sh
# Lab run of test_improv_hardware.py (passed on the rig 2026-10-13, remote mode).
#
# Rig: lab-pi5 is the Hub (inside the cage: BLE on ESPHOME_BLE_ADAPTER). The ESP32-C3 DUT, flashed with
# esp32c3_improv.yaml, sits in the cage on lab-pi5's USB (power and console). A temporary NetworkManager hotspot is the
# Wi-Fi the Hub hands the DUT over Improv, so the DUT lands on the hotspot's NAT subnet. AP_HOST picks where it runs:
#   local (the default: AP_HOST unset or "local"): on lab-pi5's own Wi-Fi, next to the test, so mDNS and the API reach
#     the DUT directly.
#   remote (AP_HOST=<ssh target>): on the CM4 bench board, cabled to lab-pi5 (lab-pi5 AP_LINK <-> CM4 AP_GATEWAY).
#     lab-pi5 routes the hotspot's subnet (AP_SUBNET) through the CM4, and the CM4's avahi reflector carries the DUT's
#     mDNS over the cable. Log in to lab-pi5 with `ssh -A` (lab-pi5 has no key of its own for the CM4).
# Everything set up here is reverted on exit: the connection is removed, Wi-Fi radio, rfkill and forwarding are left
# as found.
#
# Usage, on lab-pi5 from the repo checkout (rsynced from the Mac, never cloned):
#   tests/hardware/run_hardware.sh [pytest args]
#   AP_HOST=admin@10.99.0.2 tests/hardware/run_hardware.sh [pytest args]
# Optional: AP_IFACE (wlan0), AP_SSID (MajorDom-Improv-Test), AP_CHANNEL (6: the C3 is 2.4 GHz only),
#   PYTEST (poetry run pytest), AUTHORIZER=1 when the DUT was built with `-s authorizer boot_button` (press BOOT when
#   asked), ESPHOME_DUT_SERIAL (the C3's /dev/serial/by-id path: checked before anything is touched), and the test's
#   ESPHOME_BLE_ADAPTER, ESPHOME_DISCOVERY_TIMEOUT, ESPHOME_TEST_TIMEOUT.
#   Remote only: AP_SSH_OPTS (e.g. `-o UserKnownHostsFile=<file with the CM4 host key>`), AP_LINK (eth1),
#   AP_GATEWAY (10.99.0.2), AP_SUBNET (10.42.0.0/24: NetworkManager's shared default).
# A DUT left provisioned by an aborted run: reflash it (`esptool erase-flash`, then write the factory image).
set -eu

AP_HOST=${AP_HOST:-local}
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
# The AP host's state to restore: kept there, in root-only tmpfs
STATE=/run/esphome-hw-ap
NODNS=/etc/NetworkManager/dnsmasq-shared.d/esphome-hw-nodns.conf
AVAHI=/etc/avahi/avahi-daemon.conf

remote() { [ "$AP_HOST" != local ]; }

ap() {
    if remote; then
        # shellcheck disable=SC2086,SC2029  # AP_SSH_OPTS is a list of options; commands expand here on purpose
        ssh $AP_SSH_OPTS "$AP_HOST" "$@"
    else
        sh -c "$*"
    fi
}

# 0. The DUT is plugged in
if [ -n "${ESPHOME_DUT_SERIAL:-}" ] && [ ! -e "$ESPHOME_DUT_SERIAL" ]; then
    echo "No ESP32-C3 at $ESPHOME_DUT_SERIAL" >&2
    exit 1
fi

# 1. The CI runner owns the rig while it holds the Consul lock
if curl -s localhost:8500/v1/kv/github-runner/iot-lock | grep -q '"Session"'; then
    echo "The GitHub runner holds github-runner/iot-lock: not touching the rig" >&2
    exit 1
fi

# 2. One cage user at a time
exec 9>"$HOME/.cage-hw.lock"
flock -n 9 || { echo "Another cage session holds ~/.cage-hw.lock" >&2; exit 1; }

# 3. The access point (remote: plus the route and the mDNS reflector), all reverted whatever happens
stop_ap() {
    if remote; then sudo ip route del "$AP_SUBNET" via "$AP_GATEWAY" dev "$AP_LINK" 2>/dev/null || true; fi
    # Deleting the connection also drops NetworkManager's nm-shared table, with the forward rule inserted below
    ap "sudo nmcli connection delete $AP_CON >/dev/null 2>&1; sudo rm -f $NODNS; \
        if sudo test -f $STATE/avahi-daemon.conf; then sudo cp $STATE/avahi-daemon.conf $AVAHI; \
            sudo systemctl restart avahi-daemon; fi; \
        if sudo test -f $STATE/radio-off; then sudo nmcli radio wifi off; fi; \
        if sudo test -f $STATE/rfkill-blocked; then sudo rfkill block wifi; fi; \
        if sudo test -f $STATE/ip_forward; then sudo sysctl -qw net.ipv4.ip_forward=\$(sudo cat $STATE/ip_forward); fi; \
        sudo rm -rf $STATE" || echo "Revert the AP on $AP_HOST by hand: connection $AP_CON, $NODNS, $AVAHI" >&2
}
trap stop_ap EXIT
trap 'exit 130' INT TERM
# The hotspot must not take the AP host's uplink. NetworkManager's dnsmasq would take :53 where a DNS server (bind9
# on the CM4) holds it: DHCP only then (systemd-resolved's stub on 127.0.0.53/54 doesn't clash). The password goes
# over stdin.
SETUP="read -r PW && set -e && \
    if ip -4 route show default | grep -q 'dev $AP_IFACE '; then \
        echo '$AP_IFACE carries the default route: not making it a hotspot' >&2; exit 1; fi && \
    sudo mkdir -m 700 $STATE && \
    if [ \"\$(nmcli radio wifi)\" != enabled ]; then sudo touch $STATE/radio-off; fi && \
    if sudo rfkill list wifi | grep -q 'Soft blocked: yes'; then sudo touch $STATE/rfkill-blocked; fi && \
    sudo cp /proc/sys/net/ipv4/ip_forward $STATE/ip_forward && \
    sudo rfkill unblock wifi && sudo nmcli radio wifi on && sleep 3 && \
    if sudo ss -Hlnu 'sport = :53' | awk '{print \$4}' | grep -qv '^127\.0\.0\.5[34][%:]'; then \
        echo port=0 | sudo tee $NODNS >/dev/null; fi && \
    sudo nmcli dev wifi hotspot ifname $AP_IFACE con-name $AP_CON ssid $AP_SSID password \"\$PW\" band bg \
        channel $AP_CHANNEL >/dev/null"
if remote; then
    HUB_SUBNET=$(ip -4 -o addr show "$AP_LINK" | awk '{print $4}')  # the cable's subnet, forwarded into the hotspot
    SETUP="$SETUP && sudo cp -p $AVAHI $STATE/avahi-daemon.conf && \
        sudo nft insert rule ip nm-shared-$AP_IFACE filter_forward ip saddr $HUB_SUBNET oifname $AP_IFACE accept && \
        sudo sed -i 's/^#*enable-reflector=.*/enable-reflector=yes/' $AVAHI && sudo systemctl restart avahi-daemon"
fi
printf '%s\n' "$AP_PASSWORD" | ap "$SETUP"
if remote; then sudo ip route replace "$AP_SUBNET" via "$AP_GATEWAY" dev "$AP_LINK"; fi

# 4. The test: the Hub side runs here, against the real BLE and mDNS
[ "${AUTHORIZER:-0}" = 1 ] && echo "Press BOOT on the ESP32-C3 when the Hub asks for it (within 60 s)"
# shellcheck disable=SC2086  # PYTEST is a command line
ESPHOME_WIFI_SSID=$AP_SSID ESPHOME_WIFI_PASSWORD=$AP_PASSWORD $PYTEST -p no:cacheprovider -v \
    --include-hardware-iot -m hardware_iot_device tests/hardware "$@"
