---
title: 'Identity bridge Reach ↔ XActions'
type: feature
ticket: '1'
created: '2026-11-22'
status: built
baseline_revision: '2c3a2ec'
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

1:1 mapping between a farm registry `accounts` row and an XActions `accountPool` entry (`../XActions` @ `main-clean`, `src/scrapers/twitter/http/accountPool.js`). Mapping lives in a bridge-owned `xactions_link` table inside the registry DB; session cookies are written only into the pool entry's `cookies` column; XActions-side `locked` state folds back into `accounts.ban_status`. No shadow account system, no credential copies.

## Code Map

- `../XActions/src/scrapers/twitter/http/accountPool.js` — pool = SQLite `$XACTIONS_HOME/accounts.db` (else `~/.xactions`), WAL + busy_timeout 5000 + 0600, table `accounts(name PK, cookies NOT NULL, proxy, locked, lock_reason, locked_at, lock_until, last_used, added_at)` + `rate_limits`. Bans = `locked=1` (hard codes 32/64/89/326; soft 353 + `lock_until`). Timestamps are ms (`Date.now()`).
- `agent_reach/runtime/farm.py` — registry schema + `connect()` (WAL, perms) + `_with_busy_retry` pattern. `accounts.ban_status` exists as the lifecycle field; **no public setter**, so the bridge does its own UPDATE inside `farm.connect()`.
- `agent_reach/runtime/xactions_bridge.py` (new) — `link`/`unlink`/`resolve`/`provision_session`/`record_ban`/`sync_bans`, `XActionsPool` adapter over the real `accounts` table (column-identical DDL), `xactions_home()` mirroring `resolveCacheDir()`.
- `docs/farm-xactions-bridge.md` (new) — schema mapping, flows, threat note, 16.2 doc-id hook.
- `tests/test_xactions_bridge.py` (new) — 10 tests, real SQLite files under tmp `HOME`/`XACTIONS_HOME`.

## Tasks & Acceptance

- [x] Bridge module: `link` (default id `reach:<name>`; missing pool entry → locked placeholder `awaiting_session`), `unlink` (mapping only), `resolve`, `provision_session` (normalize cookies, require `auth_token`, unlock entry, never return secrets), `record_ban` (link row + `accounts.ban_status`; never fabricates a link), `sync_bans` (skips placeholders).
- [x] Pool adapter: `get`/`add`/`update`/`remove` against the real schema; `lock_until` applied via `ALTER` like the JS store.
- [x] Contract doc + ≥6 tests; full suite green.
- [x] No edits to `farm.py`/`dispatch.py`/existing farm tests (parallel-agent constraint) — own `_with_busy_retry` kept local in the bridge.

### Verify

`add_account("a1")` → `link("a1")` creates `reach:a1` pool entry (locked placeholder) + link row; simulate XActions ban (`pool.update(locked=True)`) → `sync_bans()` → registry `ban_status='banned'`.

## Implementation Notes

- Placeholder trick: `link` provisions a `cookies=''`, `locked=1` pool row so the mapping is concrete without a servable entry; `provision_session` is what unlocks it. `sync_bans` skips `awaiting_session` locks so unprovisioned ≠ banned.
- `record_ban` on an unlinked account updates `accounts.ban_status` only — a write-back must not fabricate a mapping.
- Env seam: `XACTIONS_HOME` (same var XActions uses) — tests point both roots at tmp.

## Verification

```bash
uv run --extra dev python -m pytest tests/test_xactions_bridge.py -q
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- `tests/test_xactions_bridge.py`: 10/10 pass (link placeholder, idempotent relink, unknown account NEED_SETUP, unlink keeps pool entry, provision writes+unlocks+no-secret-return, auth_token required + NEED_SETUP unlinked, record_ban registry+bridge, unlinked ban registry-only, sync_bans folds pool lock, pool store schema smoke).
- Full suite: see final pytest output (baseline 966 + 10 new).
- Deviations: none blocking — `sync_bans` added beyond the 5 required API calls because the verify step needs the XActions→registry direction automated, not manual.
