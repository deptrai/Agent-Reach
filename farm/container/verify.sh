#!/bin/sh
# Story 14.2 acceptance, on a Docker host (run from farm/container/):
#   REACH_ACCOUNT=acct001 REACH_BROWSER_PROXY=http://... ./verify.sh
# Gates: build → `agent-reach check` JSON in-container → RAM cap via docker stats
# → dedicated browser launch (shard) → netns kill-switch probes (14.3)
# → `x post` (manual step, needs a logged-in test account).
set -u
cd "$(dirname "$0")"
IMAGE=${FARM_IMAGE:-agent-reach-farm:1.6.1}
ACCOUNT=${REACH_ACCOUNT:?export REACH_ACCOUNT=<account-name>}
PROXY=${REACH_BROWSER_PROXY:?export REACH_BROWSER_PROXY=<proxy-url>}
CT="farm-$ACCOUNT"
SHA=${AGENT_REACH_SOURCE_SHA:-$(git -C ../.. rev-parse HEAD 2>/dev/null || echo unknown)}

echo "==> [1/6] build linux/amd64 (C7 pins: python 3.12.7 / node ${NODE_VERSION:-22.11.0} / opencli 2.0.3 / shardx 2.1.7 / agent-reach@$SHA)"
docker build --platform linux/amd64 -f Dockerfile -t "$IMAGE" --build-arg "AGENT_REACH_SOURCE_SHA=$SHA" ../..

echo "==> [2/6] run $CT (FARM_BOOT_CHECK=0: stay up pre-onboarding)"
echo "    --user root + --cap-add NET_ADMIN: entrypoint arms the netns kill-switch, drops to farm."
docker rm -f "$CT" >/dev/null 2>&1 || true
docker run -d --name "$CT" --platform linux/amd64 --memory=1536m \
    --user root --cap-add NET_ADMIN \
    -e "REACH_ACCOUNT=$ACCOUNT" -e "REACH_BROWSER_PROXY=$PROXY" \
    -e FARM_BOOT_CHECK=0 -e FARM_KILLSWITCH=1 \
    -v "$CT-home:/home/farm/.agent-reach" \
    "$IMAGE" sleep infinity

echo "==> [3/6] agent-reach check → JSON from inside the container (-u farm)"
docker exec -u farm "$CT" agent-reach check | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print("check ok:", d["ok"], "| scope:", d["scope"])'

echo "==> [4/6] RAM cap — docker stats must show a ~1.5GiB limit"
docker stats --no-stream --format '{{.Name}}  mem {{.MemUsage}}  limit {{.MemLimit}}' "$CT"

echo "==> [5/6] netns kill-switch probes (story 14.3 — contract §2 layer-2)"
echo "    rules in-container:"
docker exec "$CT" sh -c 'iptables -S OUTPUT'
echo "    a) direct egress MUST fail:"
docker exec -u farm "$CT" sh -c \
    'curl --noproxy "*" -m5 -s -o /dev/null -w "direct:%{http_code}\n" https://ifconfig.me || echo "direct BLOCKED rc=$? (expected)"'
echo "    b) direct-IP (no DNS involved) MUST fail:"
docker exec -u farm "$CT" sh -c \
    'curl --noproxy "*" -m5 -s -o /dev/null -w "directip:%{http_code}\n" http://1.1.1.1/ || echo "direct-IP BLOCKED rc=$? (expected)"'
echo "    c) DNS MUST fail (no leak through docker resolver):"
docker exec -u farm "$CT" sh -c 'getent hosts example.com || echo "DNS BLOCKED (expected)"'
echo "    d) egress THROUGH the proxy should work while the proxy is alive"
echo "       (needs a real proxy in REACH_BROWSER_PROXY):"
docker exec -u farm "$CT" sh -c \
    'curl -x "$REACH_BROWSER_PROXY" -m10 -s -o /dev/null -w "via-proxy:%{http_code}\n" https://ifconfig.me || echo "via-proxy FAILED rc=$? — dead proxy also means dead container egress (fail-closed)"'
echo "    e) loopback bridge :19825 unaffected — covered by -o lo ACCEPT"

echo "==> [6/6] dedicated browser (shard backend) + post"
docker exec -u farm "$CT" agent-reach browser setup
docker exec -u farm "$CT" agent-reach browser status
cat <<EOF
Manual gate (needs a logged-in test account in this container) —
the ticket's "x post" is channel twitter:
  docker exec -u farm -it $CT agent-reach login twitter
  docker exec -u farm     $CT agent-reach twitter post 'farm probe'
Kill-switch evidence: with the proxy stopped, EVERY probe above fails —
the container has no route out but tcp <proxy-ip>:<proxy-port>.
Then re-enable the boot gate: FARM_BOOT_CHECK=1 in accounts/$ACCOUNT.env.
EOF
