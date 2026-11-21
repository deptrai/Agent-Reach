#!/bin/sh
# Story 14.2 acceptance, on a Docker host (run from farm/container/):
#   REACH_ACCOUNT=acct001 REACH_BROWSER_PROXY=http://... ./verify.sh
# Gates: build → `agent-reach check` JSON in-container → RAM cap via docker stats
# → dedicated browser launch (shard) → `x post` (manual step, needs a logged-in
# test account).
set -u
cd "$(dirname "$0")"
IMAGE=${FARM_IMAGE:-agent-reach-farm:1.6.1}
ACCOUNT=${REACH_ACCOUNT:?export REACH_ACCOUNT=<account-name>}
PROXY=${REACH_BROWSER_PROXY:?export REACH_BROWSER_PROXY=<proxy-url>}
CT="farm-$ACCOUNT"
SHA=${AGENT_REACH_SOURCE_SHA:-$(git -C ../.. rev-parse HEAD 2>/dev/null || echo unknown)}

echo "==> [1/5] build linux/amd64 (C7 pins: python 3.12.7 / node ${NODE_VERSION:-22.11.0} / opencli 2.0.3 / shardx 2.1.7 / agent-reach@$SHA)"
docker build --platform linux/amd64 -f Dockerfile -t "$IMAGE" --build-arg "AGENT_REACH_SOURCE_SHA=$SHA" ../..

echo "==> [2/5] run $CT (FARM_BOOT_CHECK=0: stay up pre-onboarding)"
docker rm -f "$CT" >/dev/null 2>&1 || true
docker run -d --name "$CT" --platform linux/amd64 --memory=1536m \
    -e "REACH_ACCOUNT=$ACCOUNT" -e "REACH_BROWSER_PROXY=$PROXY" \
    -e FARM_BOOT_CHECK=0 \
    -v "$CT-home:/home/farm/.agent-reach" \
    "$IMAGE" sleep infinity

echo "==> [3/5] agent-reach check → JSON from inside the container"
docker exec "$CT" agent-reach check | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print("check ok:", d["ok"], "| scope:", d["scope"])'

echo "==> [4/5] RAM cap — docker stats must show a ~1.5GiB limit"
docker stats --no-stream --format '{{.Name}}  mem {{.MemUsage}}  limit {{.MemLimit}}' "$CT"

echo "==> [5/5] dedicated browser (shard backend) + post"
docker exec "$CT" agent-reach browser setup
docker exec "$CT" agent-reach browser status
cat <<EOF
Manual gate (needs a logged-in test account in this container) —
the ticket's "x post" is channel twitter:
  docker exec -it $CT agent-reach login twitter
  docker exec    $CT agent-reach twitter post 'farm probe'
Then re-enable the boot gate: FARM_BOOT_CHECK=1 in accounts/$ACCOUNT.env.
EOF
