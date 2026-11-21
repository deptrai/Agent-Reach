#!/bin/sh
# farm-netns-killswitch — story 14.3: contract §2 layer-2 (hard enforcement).
# Zero egress outside proxy_i at the network layer: OUTPUT policy DROP, the
# only accepted packets are loopback and TCP to the proxy's resolved IP:port.
# DNS (53) and DoT (853) are dead too — name resolution for the proxy itself is
# pinned into /etc/hosts before the rules land.
#
# Runs ONCE at container boot as root (compose: `user: root` + `cap_add:
# NET_ADMIN`); the entrypoint then drops to uid `farm` via runuser, so no
# in-container process keeps NET_ADMIN to undo the rules.
# Fail-closed: any failure exits non-zero → container start aborts — a dead
# container beats a leaky one (contract: "fail-closed", not "best effort").
set -eu

if [ "${FARM_KILLSWITCH:-1}" = "0" ]; then
    echo "farm: FARM_KILLSWITCH=0 — netns kill-switch DISABLED (dev escape hatch, not contract-compliant)." >&2
    exit 0
fi

command -v iptables >/dev/null 2>&1 || {
    echo "farm: iptables missing in image — cannot enforce kill-switch, fail closed." >&2
    exit 1
}
PROXY_URL=${REACH_BROWSER_PROXY:?"netns kill-switch requires REACH_BROWSER_PROXY"}

# Parse + resolve in ONE python3 pass — resolution must happen before the
# rules land (dport 53 is about to be dropped). Emits shell vars:
# PROXY_HOST, PROXY_PORT, PROXY_IPS (space-separated, both families).
PARSED=$(python3 - "$PROXY_URL" <<'PY'
import ipaddress
import shlex
import socket
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
host, port = url.hostname, url.port
if not host or port is None:
    sys.exit(f"kill-switch: proxy URL has no host:port: {sys.argv[1]!r}")
try:
    ipaddress.ip_address(host)
    ips = [host]                       # literal — no DNS needed at all
except ValueError:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        sys.exit(f"kill-switch: proxy host {host!r} does not resolve: {exc}")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        sys.exit(f"kill-switch: proxy host {host!r} resolved to nothing")
print(f"PROXY_HOST={shlex.quote(host)}")
print(f"PROXY_PORT={port}")
print("PROXY_IPS=" + shlex.quote(" ".join(ips)))
PY
)
eval "$PARSED"

# Pin the proxy hostname in /etc/hosts: post-rules there is no DNS, so the
# browser/dispatch must resolve the proxy host via files. Appended (not
# rewrite-in-place: /etc/hosts is a docker bind-mount, sed -i would fail).
# Idempotent via marker; docker regenerates the file on container restart.
if [ "$PROXY_HOST" != "" ] && ! grep -q 'farm-netns-killswitch' /etc/hosts 2>/dev/null; then
    for ip in $PROXY_IPS; do
        echo "$ip $PROXY_HOST # farm-netns-killswitch" >> /etc/hosts
    done
fi

# IPv4 OUTPUT: drop everything, whitelist the only egress allowed.
# Replies come back on INPUT (unrestricted) — no conntrack rule needed.
iptables -P OUTPUT DROP
iptables -F OUTPUT                                # idempotent: wipe, re-add
iptables -A OUTPUT -o lo -j ACCEPT                # loopback: bridge :19825, CDP :9222
iptables -A OUTPUT -p udp --dport 53 -j DROP      # DNS — no leak via any resolver
iptables -A OUTPUT -p tcp --dport 53 -j DROP
iptables -A OUTPUT -p udp --dport 853 -j DROP     # DNS-over-TLS
iptables -A OUTPUT -p tcp --dport 853 -j DROP

# IPv6: same zero-egress contract. A netns with an IPv6 stack but no ip6tables
# is a leak path — fail closed rather than leave it open.
if [ -f /proc/net/if_inet6 ]; then
    command -v ip6tables >/dev/null 2>&1 || {
        echo "farm: IPv6 stack present but ip6tables missing — fail closed." >&2
        exit 1
    }
    ip6tables -P OUTPUT DROP
    ip6tables -F OUTPUT
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -p udp --dport 53 -j DROP
    ip6tables -A OUTPUT -p tcp --dport 53 -j DROP
    ip6tables -A OUTPUT -p udp --dport 853 -j DROP
    ip6tables -A OUTPUT -p tcp --dport 853 -j DROP
fi

# The single allowed egress: TCP to each resolved proxy IP on the proxy port.
for ip in $PROXY_IPS; do
    case "$ip" in
        *:*)
            if [ -f /proc/net/if_inet6 ]; then
                ip6tables -A OUTPUT -d "$ip" -p tcp --dport "$PROXY_PORT" -j ACCEPT
            fi ;;
        *)
            iptables -A OUTPUT -d "$ip" -p tcp --dport "$PROXY_PORT" -j ACCEPT ;;
    esac
done

echo "farm: netns kill-switch armed — only egress is tcp $PROXY_HOST:$PROXY_PORT -> $PROXY_IPS"
