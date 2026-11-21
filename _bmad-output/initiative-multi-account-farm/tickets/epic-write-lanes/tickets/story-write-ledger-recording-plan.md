---
title: 'Write → ledger recording'
type: feature
ticket: '7'
created: '2026-11-22'
status: built
baseline_revision: 'be9098a9ae5d506e3c72eb5981de6f77b6858e52'
route: full
route_source: auto
risk: low
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Contract §3 — every account write action must append one `actions_log` row so `ledger_24h` can count per-account quotas. Story 12.4 deliberately left the `record_action` seam unwired; nothing calls it today.

**Approach:** Single seam at dispatch: `run_action` becomes a thin wrapper over `_run_action` (the original body, renamed). After the run returns — final envelope in hand, env window already restored — `_record_farm_write(channel.name, action_name, result)` appends `farm.record_action(account, action_name, outcome)` when `REACH_ACCOUNT` is set and `(channel, verb)` is a write. `outcome` = `"success"` on `ok:true`, else `result["error"]["code"]` (result.py vocabulary). Because the wrapper captures the envelope, the recorded outcome is the FINAL one — after backend retries and the public-web fallback.

**Why dispatch, not the channels:** one seam covers facebook + twitter + every future write channel uniformly; nested `run_action` inside a farm window (e.g. public-web attempts) still sees `REACH_ACCOUNT`; the `REACH_FARM_ROOT` pin guarantees the row lands on the operator registry, not a second registry under the swapped HOME.

**Verb → class map:** flat verb-as-class (`by_class.post`, `by_class.reply`, …) per contract §5 examples. Membership is a per-channel set `WRITE_ACTIONS` in `dispatch.py` because verb names collide across platforms: xueqiu's `quote` is a stock-price READ while twitter's `quote` is a write (12.5); twitter's current read `thread` becomes the 12.6 write. Sets pre-seeded with landing-soon verbs (`reply`, `quote`, `thread` on twitter; `message` on facebook) so 12.5/12.6 record with zero extra wiring.

**Fail-safe:** the ledger write is wrapped in `except Exception: pass` — a locked/corrupt registry must never change the action result. `REACH_ACCOUNT` unset → early return, zero overhead on the single-account path.

## Boundaries & Constraints

**Always:** only `dispatch.py` (+ new test file) changes; one ledger row per `run_action` call; reads never record; outcome vocabulary is `"success"` or an existing result.py code.

**Never:** no edits to `twitter.py`/`_twitter_write.py` (parallel 12.5 worktree); no recording inside channel code; no blocking or retry logic around the ledger write; no changes to `farm.py`/`result.py` (existing vocabulary suffices).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY | `REACH_ACCOUNT=test1` + `fb post` ok | ledger `success:1`, `by_class.post:{success:1,attempts:1}` | — |
| FAIL | write backend raises ReachError | `attempts:1`, `success:0`, row outcome = error code | row written post-restore |
| READ | `search`/`profile`/`whoami` under REACH_ACCOUNT | no row | verb not in set → early return |
| NO_ACCOUNT | REACH_ACCOUNT unset | no row, zero ledger overhead | early return |
| X-VERB | xueqiu `quote` under REACH_ACCOUNT | no row | channel not in map |
| BAD_ARGS | write verb, missing required param | `BAD_INPUT` attempt row | validation envelope still records |
| GHOST | `REACH_ACCOUNT=unknown` + write | NEED_SETUP envelope; `record_action` NEED_SETUP swallowed, no row | fail-safe wrap |
| LEDGER_DOWN | `record_action` raises | action result unchanged | `except Exception: pass` |
| NESTED | run_action inside applied window | records (env still visible; REACH_FARM_ROOT pinned) | — |
| BUSY | write while another thread owns env | `BUSY` attempt row when account exists | envelope code is valid outcome |

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests/test_farm_ledger_wiring.py -x -q` — 9 new tests
- `uv run --extra dev python -m pytest tests -x -q` — full suite (baseline: 934)

## Auto Run Result

- **Summary**: write→ledger recording wired at the dispatch seam: `run_action` wraps renamed `_run_action` and calls `_record_farm_write` with the final envelope. `WRITE_ACTIONS` — a per-channel verb map in `dispatch.py` — decides membership (verb itself is `action_class`); outcome is `"success"` or the envelope error code. Recording is skipped silently without `REACH_ACCOUNT` and wrapped so ledger failures can never break an action.
- **Files**: `agent_reach/runtime/dispatch.py` (+`WRITE_ACTIONS`, +`_record_farm_write`, `run_action`→`_run_action` rename + thin wrapper); `tests/test_farm_ledger_wiring.py` (new, 9 tests). No changes to `farm.py`, `result.py`, or any channel.
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **943 pass** (934 + 9). Farm suites (`test_farm*`, `test_farm_dispatch`, `test_farm_e2e`) all green — env window behavior unchanged.
- **Deviations**: verb set is per-channel, not the flat global set the ticket sketched — a flat set containing `quote` would record xueqiu's stock-price READ (hard rule: reads must not record). `twitter.thread` is included pre-emptively (today it's a read; 12.6 turns `x thread` into the write — until then a farm-context `x thread` read records a `thread` row; harmless, no quota consumer exists yet). Validation-failure envelopes (BAD_INPUT) also record as attempts — "every write action appends" reads naturally including rejected calls.
- **Residual risks**: if 12.5/12.6 rename their write verbs away from `reply`/`quote`/`thread`, the map needs a one-word update; a future write action on a channel absent from `WRITE_ACTIONS` records nothing until listed (deliberate — explicit over guessed).
</intent-contract>
