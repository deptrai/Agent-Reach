---
title: 'Doc-id drift monitor — cron probe + alert'
type: feature
ticket: '3'
created: '2026-11-22'
status: built
baseline_revision: '28cfb7c'
route: full
route_source: pinned
risk: medium
review: quick
review_source: pinned
lenses_ran:
  - quick
context: []
warnings: []
deferred: []
---

## Intent

Host-level cron that probes every persisted doc-id in **both** stores (Reach
`query-ids.json` + XActions query-id cache, the "endpoint.url" store) on a
6h ±30min cadence with a 30s per-probe budget, and alerts through a
configurable sink (`stdout` default / `file` / `webhook`) before a platform
rotation kills the whole farm at once. Reuses the `doctor --probe-docids`
core for the live Facebook probe. No on-demand check — that stays with
doctor (epic-write-lanes).

## Code Map

- `agent_reach/runtime/farm_drift.py` (new) — `config()` (env-only, BAD_INPUT on invalid),
  `collect()`/`_reach_ids()`/`_xactions_ids()` (both stores), `find_drift()`
  (cross-store mismatch for `x`), `_live_facebook()` (doctor core),
  `render_alert()`, `send_alerts()` (stdout/file/webhook), `run_once()`,
  `run_forever()` + `next_delay()` (interval ± jitter).
- `agent_reach/runtime/cli.py` — `farm drift [--once]` op (loop by default, `--once` for cron).
- `agent_reach/runtime/xactions_bridge.py` (existing, story 16.1) — `xactions_home()`
  for the XActions cache path; not modified.
- `agent_reach/runtime/query_ids.py` — **not modified** (story 16.2 is using it;
  interface unchanged). Read-only use of `load()`/`get()`/`config_path()`.
- `agent_reach/runtime/xactions_docids.py` (story 16.2, untracked at build time)
  — **not imported**; the cache path is mirrored locally (3 lines) so this
  story's commit does not depend on an uncommitted file.
- `docs/farm-drift-monitor.md` (new) — cadence, probe, sink, alert format, cron example.
- `tests/test_farm_drift_monitor.py` (new) — 10 tests.

## Tasks & Acceptance

- [x] `farm drift --once` probes all doc-ids in both stores; report `probes`
      lists store/platform/name/id for every entry.
- [x] Drift on either store (Reach edit or XActions cache edit) → alert naming
      platform + operation + both ids.
- [x] Sinks: stdout (default), file (`REACH_DRIFT_FILE`, default
      `~/.agent-reach/drifts.log`), webhook (`REACH_DRIFT_WEBHOOK_URL`, JSON POST,
      `REACH_DRIFT_TIMEOUT`). Sink failure → `sink_errors`, never aborts.
- [x] Cadence: `REACH_DRIFT_INTERVAL` (21600) ± `REACH_DRIFT_JITTER` (1800),
      `REACH_DRIFT_TIMEOUT` (30); invalid env → BAD_INPUT.
- [x] Live Facebook probe opt-in via `REACH_DRIFT_LIVE=1`, reusing
      `manage.doctor(probe_doc_ids=True)`.
- [x] Docs + 10 tests; full suite green.

### Verify

Cron run → probe log lists doc-ids of both stores; fake one doc-id (either
store) → alert fires to the configured sink on the next cycle.

## Implementation Notes

- Drift signal is **cross-store mismatch**, not a live platform call: XActions'
  cache refreshes from x.com's own bundles, so it acts as the live-ish
  reference for X; Reach is the single source of truth (16.2 syncs one way).
  Facebook has no second store, so its rotation is only caught by the opt-in
  live probe.
- `farm drift` loops by default (6h ±30min); `--once` is the cron/manual entry
  point. Loop mode validates config once up front so a typo'd env exits with
  BAD_INPUT instead of sleeping forever.
- Sink `stdout` writes alert lines to stdout *before* the JSON envelope, so a
  cron log captures both; the envelope stays the last line (`tail -1 | jq`).

## Verification

```bash
uv run --extra dev python -m pytest tests/test_farm_drift_monitor.py -q
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- `tests/test_farm_drift_monitor.py`: 10/10 pass — both-store probe surface,
  Reach-side and XActions-side drift, stdout/file/webhook sinks, no-drift
  silence, BAD_INPUT config (bad sink / webhook without URL / non-numeric
  interval), live-probe stale alert, jitter bounds.
- Full suite: 1048 passed (`uv run --extra dev python -m pytest tests -x -q`,
  final tree with story 16.2 committed).
- Deviations: live platform probe is opt-in (`REACH_DRIFT_LIVE=1`) and
  Facebook-only, because it needs a logged-in browser session; the default
  cadence check is the deterministic cross-store comparison. XActions has no
  `endpoint.url` file — the query-id cache (`query-ids.json`) is that store
  (documented in `docs/farm-drift-monitor.md`). `xactions_docids.py` (16.2)
  is not imported; the cache path is mirrored locally (3 lines).
- Commit note: story 16.2's commit `28cfb7c` landed while this story was in
  flight and swept up the shared `agent_reach/runtime/cli.py` hunk (the
  `farm drift [--once]` wiring). That wiring is therefore already in HEAD;
  this story's own commit carries `farm_drift.py`, the tests, the docs page
  and this plan file. Functionally complete; attribution of the cli.py hunk
  sits under 16.2's commit message.
