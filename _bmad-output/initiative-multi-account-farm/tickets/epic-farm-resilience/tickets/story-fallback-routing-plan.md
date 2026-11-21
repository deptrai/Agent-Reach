---
title: 'Fallback routing — mutation chết → xactions HTTP lane'
type: feature
ticket: '2'
created: '2026-11-22'
status: built
baseline_revision: 'e4a866a'
route: full
route_source: pinned
risk: high
review: quick
review_source: pinned
lenses_ran:
  - quick
context: []
---

## Goal

When a twitter write (`post`/`reply`/`quote`/`thread`) dies with
`PERSISTED_QUERY_DEAD` or an upstream schema error, dispatch routes it once
to the XActions HTTP lane — never into `RATE_LIMITED`/`PLATFORM_BLOCKED`
semantics. Kill-switch + quota gates still run before the lane, the lane's
success counts against the cap, and egress is the pool entry's per-account
proxy.

## Design decisions

- **Invocation surface** — a one-shot `node --input-type=module` subprocess
  importing `src/scrapers/twitter/http` (`createAccountPool` +
  `postTweet`/`replyToTweet`/`quoteTweet`/`postThread`) from the XActions
  checkout (`XACTIONS_REPO`, else `../XActions`). No existing surface does
  both pool selection and HTTP posting: `xactions client post` reads the
  legacy single `cookies.json`; the MCP `x_post_tweet` text path uses a
  browser; `pool.acquire()` cannot pin an account name. Repos stay
  decoupled — only the child JSON stdout is trusted.
- **Trigger set** — `dispatch.FALLBACK_TRIGGERS = {PERSISTED_QUERY_DEAD}`
  plus `UPSTREAM_BROKEN` messages naming a schema rejection (`schema`,
  `variables`, `fieldnotdefined` markers). `RATE_LIMITED`,
  `PLATFORM_BLOCKED`, `QUOTA_EXCEEDED`, `NEED_LOGIN` deliberately excluded.
- **Proxy path** — the pool entry's `accounts.proxy` column (populated by
  `bridge.link()` from the registry row) is dialed via
  `TwitterHttpClient`→`undici.ProxyAgent` inside XActions — confirmed in
  `accountPool.js` `clientFor()`, no env var needed. The lane resolves the
  pool DB from `XACTIONS_HOME` or `<operator home>/.xactions/accounts.db`
  because `HOME` inside the dispatch window is the account's private home;
  the child's `HOME` is set to the operator home (parent of
  `REACH_FARM_ROOT`) so `XACTIONS_HOME` never inherits the swapped root.
- **Guardrails** — the fallback runs inside the env window, after
  `_enforce_proxy_kill_switch` and `_enforce_write_quota`, and a lane
  success lands as a normal `success` row via `run_action`'s
  `_record_farm_write` — no ledger schema change needed (`via=fallback` is
  visible in `meta.attempts` on the envelope; the `outcome` column stays
  canonical).
- **Doc-id sync** — Reach `query-ids.json` is the single source of truth
  (it is the file the lanes replay and `doctor --probe-docids` probes).
  XActions has no `endpoint.url` file: its `queryIds.js` resolves
  cache-first from `$XACTIONS_HOME/query-ids.json`, so
  `agent-reach farm sync-docids [--apply]` copies Reach's `x` section into
  that cache — dry-run by default, X ops only, XActions-only entries kept.

## Files

- `agent_reach/runtime/xactions_lane.py` — lane runner (subprocess +
  payload + result classification).
- `agent_reach/runtime/xactions_docids.py` — `plan()`/`apply()` sync.
- `agent_reach/runtime/dispatch.py` — `FALLBACK_TRIGGERS`,
  `_schema_error()`, `_xactions_fallback()`, one-call hook after the
  backend loop.
- `agent_reach/runtime/cli.py` — `farm sync-docids [--apply]`, HELP line.
- `tests/test_farm_fallback.py` — 10 tests (stubbed subprocess; nothing
  posts).
- `docs/farm-xactions-bridge.md` — §6 fallback lane, §7 doc-id sync.

## Verify

```bash
uv run --extra dev python -m pytest tests/test_farm_fallback.py -q
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- `tests/test_farm_fallback.py`: 10/10 pass (PQ_DEAD routes, schema-error
  routes, RATE_LIMITED/QUOTA_EXCEEDED do not, no-link surfaces original
  error, lane success counts toward cap + trips it next run, dead proxy
  blocks before lane, child HOME is operator home, sync dry-run/apply
  round-trip, facebook ids never cross).
- Full suite: 1038 passed.
- Deviations: none blocking — `_SCHEMA_ERROR_MARKERS` is a deliberate narrow
  trigger beyond the single named code; `via=fallback` rides the envelope
  `meta.attempts` trail rather than a new ledger column (schema untouched).
