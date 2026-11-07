#!/bin/sh
# ===========================================================================
# wg_endpoint_setup.sh — the ONE step the framework cannot do for you.
#
# Layer 2 (SSH into the guests) needs a path from the control node to the
# internal bridge where the guests live. The tunnel CLIENT runs on the control
# node and is converged by configure/tasks/tunnel.yml. This script is the
# ENDPOINT, and the endpoint lives on the Proxmox host — which the PVE API
# deliberately cannot configure (exec answers 501, and this framework never uses
# SSH or pct against the host).
#
# Run it ON THE PROXMOX HOST, as root, once per lab:
#
#     sh wg_endpoint_setup.sh <CONTROL_NODE_PUBLIC_KEY>
#
# The public key is NOT secret: `ansible-playbook playbooks/connect.yml` prints
# it, and so does the failure message of configure/site.yml when there is no
# handshake. Never pass a PRIVATE key to this script.
#
# Idempotent: an existing endpoint key is kept, the peer is rewritten, and the
# tunnel is re-applied. It prints the endpoint's PUBLIC key, which goes into
# pve_tunnel.endpoint_public_key in inventory/group_vars/all.yml.
#
# usage: wg_endpoint_setup.sh <CLIENT_PUBLIC_KEY> [SERVER_ADDRESS] [LISTEN_PORT] [GUEST_BRIDGE] [NAME] [EXTRA_ROUTES]
#
# EXTRA_ROUTES is a comma-separated list of `<network>=<gateway>` pairs, for the
# networks that live BEHIND a router inside the lab. The endpoint can reach the
# router itself (it is on the guest bridge) but has no idea where the networks
# behind it are, so without these the control node reaches the router and nothing
# past it. They are added to the tunnel's PostUp/PostDown, so they exist exactly
# while the tunnel does.
#
#   ... vmbr1 wg-lab "10.30.0.0/24=10.10.10.50"
# ===========================================================================
set -eu

CLIENT_PUBKEY="${1:-}"
TUNNEL_SERVER="${2:-10.99.99.1/24}"
LISTEN_PORT="${3:-51820}"
GUEST_BRIDGE="${4:-vmbr1}"
NAME="${5:-wg-lab}"
EXTRA_ROUTES="${6:-}"

[ -n "$CLIENT_PUBKEY" ] || {
  echo "usage: $0 <control-node-public-key> [server-address] [port] [guest-bridge] [name] [\"net=gw,net=gw\"]" >&2
  exit 2
}

# Build the route lines that go into the tunnel's own configuration, so the
# routes live and die with the tunnel instead of being a separate thing to keep
# in sync.
ROUTE_UP=""
ROUTE_DOWN=""
if [ -n "$EXTRA_ROUTES" ]; then
  OLDIFS=$IFS
  IFS=','
  for pair in $EXTRA_ROUTES; do
    net="${pair%%=*}"
    gw="${pair#*=}"
    [ -n "$net" ] && [ -n "$gw" ] && [ "$net" != "$gw" ] || continue
    ROUTE_UP="$ROUTE_UP
PostUp   = ip route replace $net via $gw"
    ROUTE_DOWN="$ROUTE_DOWN
PostDown = ip route del $net via $gw"
  done
  IFS=$OLDIFS
fi

WG_DIR=/etc/wireguard
# 10.99.99.1/24 -> the peer gets 10.99.99.2/32, and the tunnel subnet is
# 10.99.99.0/24. Derived, so the two sides cannot disagree about addressing.
CLIENT_ADDR=$(echo "$TUNNEL_SERVER" | awk -F'[./]' '{print $1"."$2"."$3".2/32"}')
SUBNET=$(echo "$TUNNEL_SERVER" | awk -F'[./]' '{print $1"."$2"."$3".0/24"}')

command -v wg >/dev/null 2>&1 || {
  apt-get update -qq
  apt-get install -y wireguard iptables
}

umask 077
mkdir -p "$WG_DIR"
if [ ! -f "$WG_DIR/$NAME-server.key" ]; then
  wg genkey > "$WG_DIR/$NAME-server.key"
  wg pubkey < "$WG_DIR/$NAME-server.key" > "$WG_DIR/$NAME-server.pub"
fi

cat > "$WG_DIR/$NAME.conf" <<EOF
[Interface]
Address = $TUNNEL_SERVER
ListenPort = $LISTEN_PORT
PrivateKey = $(cat "$WG_DIR/$NAME-server.key")
PostUp   = iptables -t nat -A POSTROUTING -s $SUBNET -o $GUEST_BRIDGE -j MASQUERADE$ROUTE_UP
PostDown = iptables -t nat -D POSTROUTING -s $SUBNET -o $GUEST_BRIDGE -j MASQUERADE$ROUTE_DOWN

[Peer]
# Control node (public key only)
PublicKey = $CLIENT_PUBKEY
AllowedIPs = $CLIENT_ADDR
EOF

# The host has to forward between the tunnel and the guest bridge.
sysctl -q -w net.ipv4.ip_forward=1
printf 'net.ipv4.ip_forward=1\n' > "/etc/sysctl.d/99-$NAME.conf"

# Accept the tunnel's UDP port. If OVH's own firewall is active, allow UDP there too.
iptables -C INPUT -p udp --dport "$LISTEN_PORT" -j ACCEPT 2>/dev/null ||
  iptables -I INPUT -p udp --dport "$LISTEN_PORT" -j ACCEPT

wg-quick down "$NAME" 2>/dev/null || true
wg-quick up "$NAME"
systemctl enable "wg-quick@$NAME" >/dev/null 2>&1 || true

echo
echo "Endpoint public key -> pve_tunnel.endpoint_public_key:"
cat "$WG_DIR/$NAME-server.pub"
echo
wg show "$NAME"