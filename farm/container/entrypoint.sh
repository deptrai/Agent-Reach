#!/bin/sh
# farm-entrypoint — 1 account : 1 container (story 14.2).
# Story 14.3: when launched as root (compose `user: root` + cap_add NET_ADMIN)
# the netns egress kill-switch is applied first, then the script re-execs
# itself as uid `farm` — no in-container process keeps NET_ADMIN afterwards.
set -u

if [ "$(id -u)" = "0" ]; then
    /usr/local/bin/farm-netns-killswitch
    [ "${FARM_KILLSWITCH:-1}" != "0" ] && export FARM_KILLSWITCH_APPLIED=1
    exec runuser -u farm -- "$0" "$@"
elif [ "${FARM_KILLSWITCH:-1}" != "0" ] && [ "${FARM_KILLSWITCH_APPLIED:-0}" != "1" ]; then
    echo "farm: FARM_KILLSWITCH=1 but uid $(id -u) is not root — cannot install" >&2
    echo "farm: the netns kill-switch; failing closed (contract §2 layer-2)." >&2
    echo "farm: run with --user root --cap-add NET_ADMIN, or FARM_KILLSWITCH=0 to bypass." >&2
    exit 1
fi

# The per-account HOME volume starts empty: seed the pinned OpenCLI extension
# once (baked at build time — runtime never downloads it).
EXT_DIR="${HOME:-/home/farm}/.agent-reach/browser/extension"
if [ ! -f "$EXT_DIR/manifest.json" ] && [ -d /opt/farm-seed/extension ]; then
    mkdir -p "$EXT_DIR"
    cp -a /opt/farm-seed/extension/. "$EXT_DIR/"
fi

# Boot health probe: `agent-reach check` emits the JSON the fleet manager
# consumes; its exit code signals health (0 = every configured channel passed).
# On a fresh account nothing is configured yet → check fails → container exits.
# Set FARM_BOOT_CHECK=0 to bypass during onboarding/login, back to 1 after.
#
# Story 14.3: with the netns kill-switch armed, direct egress is dead BY
# DESIGN — every direct probe inside `agent-reach check` fails on DNS, so the
# check cannot pass and is not the health signal. Under layer-2 the only path
# that exists is the proxy, so the boot gate becomes a TCP handshake against
# proxy host:port (the same probe layer-1 dispatches run). Proxy dead at boot
# → exit non-zero → `restart: unless-stopped` retries until it is back.
if [ "${FARM_BOOT_CHECK:-1}" != "0" ]; then
    if [ "${FARM_KILLSWITCH_APPLIED:-0}" = "1" ]; then
        echo "farm: kill-switch armed — boot probe = proxy TCP handshake" \
             "(agent-reach check direct probes are dead by design)." >&2
        python3 - <<'PY'
import os, sys
from agent_reach.runtime.dispatch import _proxy_alive
try:
    _proxy_alive(os.environ["REACH_BROWSER_PROXY"])
except Exception as exc:  # noqa: BLE001 - dead proxy = dead container, fail closed
    sys.exit(f"farm: proxy dead at boot: {type(exc).__name__}: {exc}")
print("farm: proxy handshake OK")
PY
        rc=$?
    else
        agent-reach check
        rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        echo "farm: boot check failed (rc=$rc); container exits." >&2
        echo "farm: set FARM_BOOT_CHECK=0 to keep the container up during onboarding." >&2
        exit "$rc"
    fi
fi

exec "$@"
