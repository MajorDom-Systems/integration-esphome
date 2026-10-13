#!/bin/sh
# The router side of run_hardware.sh, on lab-pi5 (started by it, not by hand): holds the cage, brings up the hotspot the
# DUT joins and routes it to the Hub over the cable, then keeps everything up until its stdin closes.
#
# stdin: line 1 is the hotspot password; EOF after it (the run finished, was interrupted or lost its ssh session) tears
# everything down: connection, forward rules, reflector, Wi-Fi radio, rfkill and forwarding are left as found.
# stdout: "ready" once the hotspot is up. Settings come from run_hardware.sh's environment (no defaults here).
set -eu

: "${AP_IFACE:?}" "${AP_SSID:?}" "${AP_CHANNEL:?}" "${HUB_LINK:?}" "${ESPHOME_DUT_SERIAL:?}"
AP_CON=esphome-hw-ap
# lab-pi5's state to restore, in root-only tmpfs
STATE=/run/esphome-hw-ap
NODNS=/etc/NetworkManager/dnsmasq-shared.d/esphome-hw-nodns.conf
AVAHI=/etc/avahi/avahi-daemon.conf

# 0. The DUT is plugged in
if [ ! -e "$ESPHOME_DUT_SERIAL" ]; then
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

IFS= read -r AP_PASSWORD
HUB_SUBNET=$(ip -4 -o addr show "$HUB_LINK" | awk '{print $4}')  # the cable's subnet, forwarded into the hotspot
[ -n "$HUB_SUBNET" ] || { echo "$HUB_LINK has no IPv4 address: is the Hub's cable up?" >&2; exit 1; }
if ip -4 route show default | grep -q "dev $AP_IFACE "; then
    echo "$AP_IFACE carries the default route: not making it a hotspot" >&2
    exit 1
fi

# 3. The hotspot, the Hub's way into it and the mDNS reflector, all reverted whatever happens
stop_ap() {
    if sudo test -f $STATE/docker-user; then
        sudo iptables -D DOCKER-USER -i "$HUB_LINK" -o "$AP_IFACE" -j ACCEPT
        sudo iptables -D DOCKER-USER -i "$AP_IFACE" -o "$HUB_LINK" -j ACCEPT
    fi
    # Deleting the connection also drops NetworkManager's nm-shared table, with the forward rule inserted below
    sudo nmcli connection delete $AP_CON >/dev/null 2>&1 || true
    sudo rm -f $NODNS
    if sudo test -f $STATE/avahi-daemon.conf; then
        sudo cp $STATE/avahi-daemon.conf $AVAHI && sudo systemctl restart avahi-daemon
    fi
    if sudo test -f $STATE/radio-off; then sudo nmcli radio wifi off; fi
    if sudo test -f $STATE/rfkill-blocked; then sudo rfkill block wifi; fi
    if sudo test -f $STATE/ip_forward; then sudo sysctl -qw net.ipv4.ip_forward="$(sudo cat $STATE/ip_forward)"; fi
    sudo rm -rf $STATE
}
revert() {
    status=$?
    stop_ap || echo "Revert lab-pi5 by hand: connection $AP_CON, DOCKER-USER rules, $NODNS, $AVAHI, $STATE" >&2
    exit "$status"  # not the trap's own
}
trap revert EXIT
trap 'exit 130' INT TERM HUP
sudo mkdir -m 700 $STATE
if [ "$(nmcli radio wifi)" != enabled ]; then sudo touch $STATE/radio-off; fi
if sudo rfkill list wifi | grep -q 'Soft blocked: yes'; then sudo touch $STATE/rfkill-blocked; fi
sudo cp /proc/sys/net/ipv4/ip_forward $STATE/ip_forward
sudo cp -p $AVAHI $STATE/avahi-daemon.conf
sudo rfkill unblock wifi
sudo nmcli radio wifi on
sleep 3
# NetworkManager's dnsmasq would take :53 where a DNS server holds it: DHCP only then (systemd-resolved's stub on
# 127.0.0.53/54 doesn't clash)
if sudo ss -Hlnu 'sport = :53' | awk '{print $4}' | grep -qv '^127\.0\.0\.5[34][%:]'; then
    echo port=0 | sudo tee $NODNS >/dev/null
fi
sudo nmcli dev wifi hotspot ifname "$AP_IFACE" con-name $AP_CON ssid "$AP_SSID" password "$AP_PASSWORD" band bg \
    channel "$AP_CHANNEL" >/dev/null
sudo nft insert rule ip "nm-shared-$AP_IFACE" filter_forward ip saddr "$HUB_SUBNET" oifname "$AP_IFACE" accept
# Docker's FORWARD policy is drop: let the cable and the hotspot through it
if sudo iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    sudo touch $STATE/docker-user
    sudo iptables -I DOCKER-USER -i "$HUB_LINK" -o "$AP_IFACE" -j ACCEPT
    sudo iptables -I DOCKER-USER -i "$AP_IFACE" -o "$HUB_LINK" -j ACCEPT
fi
sudo sed -i 's/^#*enable-reflector=.*/enable-reflector=yes/' $AVAHI
sudo systemctl restart avahi-daemon

echo ready
while IFS= read -r _; do :; done
