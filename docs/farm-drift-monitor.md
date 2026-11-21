# Farm doc-id drift monitor — cron probe + alert

Persisted GraphQL doc-ids are **global per platform**: when X or Facebook
rotates one, every account in the farm starts failing at once. The drift
monitor (story 16.3, `agent_reach/runtime/farm_drift.py`) is the early-warning
layer — it enumerates every persisted doc-id in **both** stores on a schedule
and alerts before the rotation is discovered as a wall of failed writes.

This is a *proactive* probe. The on-demand config check stays with
`agent-reach doctor --probe-docids` (epic-write-lanes); the monitor does not
replace it and does not change any write lane's default behaviour.

## The two stores

| Store | Path | Shape |
|---|---|---|
| Reach | `$HOME/.agent-reach/query-ids.json` (`state.root()`) | `channel → name → id` (`{"facebook": {...}, "x": {...}}`) |
| XActions | `$XACTIONS_HOME/query-ids.json` (else `~/.xactions/`) | `{"operations": {name: {"queryId", "operationType"}}}` |

XActions has **no separate `endpoint.url` file**: `endpoints.js` builds the
GraphQL URL (`/i/api/graphql/<queryId>/<operationName>`) from the id that
`queryIds.js resolveOperation()` returns — cache first, then the hardcoded
table. The cache is refreshed from x.com's own bundles, so it doubles as a
live-ish reference for the X side. Facebook doc-ids exist only in Reach.

## Cadence

Host-level cron — doc-ids are global, so **one** cron entry covers the whole
farm. Default spec: every **6h**, jitter **±30min**, per-probe budget **30s**.

```cron
# /etc/cron.d/agent-reach-drift — one probe per cycle, jitter applied by the scheduler host
17 */6 * * *  luis  cd /home/luis/Agent-Reach && /usr/bin/env agent-reach farm drift --once >> /var/log/agent-reach/drift.log 2>&1
```

Or let the command schedule itself (same defaults, jittered sleep between
cycles):

```sh
agent-reach farm drift            # loops forever: 6h ± 30min
agent-reach farm drift --once     # single cycle (what cron should call)
```

| Env | Default | Meaning |
|---|---|---|
| `REACH_DRIFT_INTERVAL` | `21600` | seconds between cycles (loop mode) |
| `REACH_DRIFT_JITTER` | `1800` | ±seconds of jitter on the interval |
| `REACH_DRIFT_TIMEOUT` | `30` | seconds per probe/webhook call |
| `REACH_DRIFT_LIVE` | off | `1` enables the live Facebook probe (needs a logged-in browser session) |

Invalid values (`REACH_DRIFT_SINK` not in `stdout|file|webhook`, a non-numeric
interval, `webhook` without a URL, …) are `BAD_INPUT` — the monitor refuses to
run silently with a typo'd config.

## What a cycle does

1. **Collect** every persisted doc-id from both stores (`probes` in the
   report lists them all — store, platform, name, id).
2. **Cross-store check** (no network): for every `x` operation both stores
   persist, the ids must match. Story 16.2 syncs Reach → XActions
   (`agent-reach farm sync-docids --apply`), so a difference means at least
   one store is stale — and XActions' cache refreshes from live bundles, so
   it usually knows first.
3. **Live probe** (opt-in, `REACH_DRIFT_LIVE=1`): reuses the doctor core
   (`manage.doctor(probe_doc_ids=True)` → `query_ids.probe_facebook`) for the
   Facebook persisted query. Off by default because it needs a logged-in
   browser; a headless host reports `unprobed`, never a false drift.
4. **Alert** through the configured sink when `drifted` is non-empty.

## Alert sinks

`REACH_DRIFT_SINK`:

- `stdout` (**default**) — one alert line per drift on stdout, followed by the
  JSON summary envelope. Cron captures both in the same log.
- `file` — append alert lines to `REACH_DRIFT_FILE`
  (default `~/.agent-reach/drifts.log`).
- `webhook` — `POST {"alerts": [...]}` as JSON to `REACH_DRIFT_WEBHOOK_URL`
  (required when this sink is selected), timeout `REACH_DRIFT_TIMEOUT`.

Alert format — platform, operation and every id involved:

```text
[doc-id drift] x/CreateTweet: reach=abc123… xactions=zzz999… (store-mismatch)
[doc-id drift] facebook/post_create: doc_id=29221… verdict=single-account (live probe)
```

Sink failures (unwritable file, dead webhook) land in `sink_errors` of the
report and **never** abort the probe — a broken pager must not blind the
monitor.

## Reading the report

`agent-reach farm drift --once` prints one JSON envelope:

```json
{"schema_version": 1, "ok": true, "action": "farm drift",
 "result": {"at": "…", "cadence": {"interval": 21600, "jitter": 1800, "timeout": 30.0},
            "sink": "stdout",
            "stores": {"reach": "…/query-ids.json", "xactions": "…/query-ids.json"},
            "probes": [{"store": "reach", "platform": "x", "name": "CreateTweet", "doc_id": "…"}, …],
            "drifted": [{"kind": "store-mismatch", "platform": "x", "name": "CreateTweet",
                         "reach_id": "…", "xactions_id": "…"}],
            "alerts": ["[doc-id drift] x/CreateTweet: …"],
            "sink_errors": [], "live": null}}
```

`probes` is the probe log: it must list the doc-ids of **both** stores every
cycle. `drifted` empty ⇒ no alert was sent.

## Verifying

```sh
# no drift: both stores agree → no alert, empty drifted
agent-reach farm drift --once

# fake a rotation on either side → alert fires on the next cycle
python - <<'PY'
import json, os
p = os.path.expanduser("~/.agent-reach/query-ids.json")
doc = json.load(open(p)); doc["x"]["CreateTweet"] = "FAKE-ID"
json.dump(doc, open(p, "w"))
PY
agent-reach farm drift --once   # → [doc-id drift] x/CreateTweet: …
```

Restore the real id afterwards (`agent-reach farm sync-docids --apply`
re-aligns Reach → XActions; edit `query-ids.json` for the reverse).

## Files

- `agent_reach/runtime/farm_drift.py` — config, store readers, drift detection, sinks, loop.
- `agent_reach/runtime/cli.py` — `agent-reach farm drift [--once]`.
- `tests/test_farm_drift_monitor.py` — 10 tests (both stores, both drift directions, all three sinks, no-drift silence, BAD_INPUT config, live probe, jitter bounds).
