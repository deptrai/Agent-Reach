#!/bin/sh
# farm-entrypoint — 1 account : 1 container (story 14.2).
set -u

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
if [ "${FARM_BOOT_CHECK:-1}" != "0" ]; then
    agent-reach check
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "farm: boot check failed (rc=$rc); container exits." >&2
        echo "farm: set FARM_BOOT_CHECK=0 to keep the container up during onboarding." >&2
        exit "$rc"
    fi
fi

exec "$@"
