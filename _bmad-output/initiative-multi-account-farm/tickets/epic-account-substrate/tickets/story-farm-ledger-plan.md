---
title: 'Farm ledger'
type: feature
ticket: '5'
created: '2026-11-21'
status: built
baseline_revision: e1a70c5201a65b81c054736f8ed87bb9a49f1ebb
route: full
route_source: auto
risk: low
review: quick
review_source: pinned
lenses_ran: [quick]
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/farm-account-contract.md'
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** The farm registry has no action ledger — nothing records per-account
action outcomes, so rolling-24h quotas (and every later farm epic) have no
counting source. Story 11.5 delivers it: the `actions_log` table, a
`record_action` API, a rolling-24h query, and the `farm account ledger <name>`
CLI that is still a BAD_INPUT stub.

**Approach:** Extend `agent_reach/runtime/farm.py` (schema + `record_action` +
`ledger_24h`), wire the `ledger` sub-branch in `_cmd_farm` in
`agent_reach/runtime/cli.py`, and extend `tests/test_farm.py`. Semantics are
fixed by `docs/farm-account-contract.md` §3/§5 — read it first.

## Boundaries & Constraints

**Always:**
- `actions_log` DDL lives in `connect()`'s schema (CREATE TABLE IF NOT EXISTS):
  `account TEXT NOT NULL`, `ts INTEGER NOT NULL` (unix epoch),
  `action_class TEXT NOT NULL`, `outcome TEXT NOT NULL`; plus
  `CREATE INDEX IF NOT EXISTS` on `(account, ts)`. Contract §5 shows the same shape.
- `record_action(account, action_class, outcome)` — one INSERT, one short-lived
  transaction, one connection per call via `connect()`; validates name via
  `_validate_name`; unknown account → `NEED_SETUP` (same message/hint style as
  `account_env`); empty/non-str `action_class` or `outcome` → `BAD_INPUT`.
- Rolling-24h query counts only `outcome='success'` toward `success`; ALL rows
  in the window count toward `attempts` (successes included — attempts is the
  total). Window = `ts >= int(time.time()) - 86400`.
- WAL rules (contract §3): `PRAGMA busy_timeout=5000` on every `connect()`;
  on `sqlite3.OperationalError` "database is locked" after the timeout, retry a
  small bounded number of times, then `ReachError(UPSTREAM_BROKEN, ...)`.
- CLI: `farm account ledger <name>` replaces the stub → `_mgmt("farm account
  ledger", farm.ledger_24h, ns.name)`; usage guard mirrors `env`'s branch.
- Stdlib only (`sqlite3`), no new deps; privacy model unchanged.

**Never:**
- No caps/warm-up enforcement (epic-account-guardrails); no outcome-vocabulary
  validation beyond non-empty (any `result.py` error code is legal).
- No `ban_status`/dispatch/`result.py` changes; no schema change to `accounts`.
- FILE BOUNDARIES: only `agent_reach/runtime/farm.py`,
  `agent_reach/runtime/cli.py`, `tests/test_farm.py`. Do NOT touch
  `dispatch.py`, `result.py`, `state.py`, or other test files.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| happy path | 3×success + 2×fail on test1, all fresh | `ledger_24h`/`farm account ledger test1` → success=3, attempts=5 | none |
| by_class | successes split across action_classes | `by_class` maps each class → `{success, attempts}` | none |
| empty ledger | account exists, no rows | `success=0, attempts=0, by_class={}` | none |
| stale rows | rows with `ts` older than 24h | excluded from both counters | none |
| unknown account | `record_action` or `ledger_24h` on unregistered name | — | `NEED_SETUP`, code 1 via CLI |
| bad input | bad name, empty `action_class`/`outcome` | — | `BAD_INPUT` |
| ledger usage | `farm account ledger` (no name) / extra args | — | `BAD_INPUT` usage line |

</intent-contract>

## Code Map

- `agent_reach/runtime/farm.py` — extend. `connect()` (:50) holds `_SCHEMA` (:28,
  single CREATE TABLE string executed via `conn.execute`); switch to
  `conn.executescript` (or loop) for the added table+index and add
  `PRAGMA busy_timeout=5000` after the journal_mode check. Reuse
  `_validate_name` (:70), `ReachError`, and the `BAD_INPUT`/`NEED_SETUP`/
  `UPSTREAM_BROKEN` imports (:23). `account_env` (:140) shows the NEED_SETUP
  idiom and `closing(connect())` pattern.
- `agent_reach/runtime/cli.py` — `_cmd_farm` (:557): `ns.op == "account"`, add
  `ns.sub == "ledger"` branch after `env` (:580-584); update the valid-ops
  string (:585) and the HELP farm line (:63).
- `tests/test_farm.py` — extend. `_run(capsys, argv)` helper (:18);
  autouse `HOME` isolation comes from `tests/conftest.py`. Old rows can be
  backdated by inserting directly via `closing(farm.connect())` (the
  `test_env_row_without_proxy` pattern, :154).

## Tasks & Acceptance

**Execution:**
- [ ] `agent_reach/runtime/farm.py` — `actions_log` schema+index in `connect()`,
      `PRAGMA busy_timeout=5000`, `record_action`, `ledger_24h`, bounded BUSY
      retry → `UPSTREAM_BROKEN` — ledger is R5's counting source.
- [ ] `agent_reach/runtime/cli.py` — `ledger` sub-branch + usage/HELP strings —
      un-stubs `farm account ledger <name>`.
- [ ] `tests/test_farm.py` — matrix rows: happy path via CLI (3s+2f → 3/5),
      by_class grouping, empty ledger zeros, stale-row exclusion, NEED_SETUP on
      record+ledger, BAD_INPUT cases, busy_timeout pragma present.

**Acceptance Criteria:**
- Given a registered account with 3 success + 2 fail rows inside 24h, when
  `farm account ledger <name>` runs, then it prints one JSON envelope with
  `result.success == 3` and `result.attempts == 5`.
- Given rows older than 24h, when the ledger query runs, then they contribute
  to neither counter.
- Given an unregistered name, when record or ledger is called, then the error
  is `NEED_SETUP` (never silent success).
- Given a fresh account with no rows, when the ledger is read, then counters
  are zero and `by_class` is empty.

## Implementation Notes

- `record_action` trả về row vừa insert (`{account, ts, action_class, outcome}`) — tiện cho caller/test kiểm ts.
- `_with_busy_retry` bọc cả write (`record_action`) và read (`ledger_24h`) sau patch review; contract chỉ bắt buộc writer nhưng crash-thay-vì-envelope là defect thật.
- Index chọn `(account, ts)` theo task brief (contract §5 recommend `(account, action_class, ts)` — không blocking; query hiện filter account+ts rồi group).

## Plan Change Log

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

- verdicts: 1 finding — high 0, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` `ledger_24h` bypassed `_with_busy_retry` — DB locked past `busy_timeout` raised raw `sqlite3.OperationalError`; `_mgmt` only catches `ReachError`, so the CLI crashed with a traceback instead of an `UPSTREAM_BROKEN` JSON envelope. Patched: `ledger_24h` DB body (connect + `_require_account` + query) now wrapped in `_with_busy_retry(_query, "farm account ledger")`; helper gained a `what` param (default `"registry"`); `record_action` unchanged.

## Design Notes

Ledger result shape (minimal, per ticket): `{"account", "window_hours": 24,
"success", "attempts", "by_class": {<class>: {"success", "attempts"}}}`.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — baseline 838 + new tests, all pass
- Live e2e on scratch HOME: `farm account add`, 5 `record_action` calls
  (3 success/2 fail), `farm account ledger <name>` → success=3, attempts=5.

## Auto Run Result

**Outcome:** `farm account ledger <name>` live — `actions_log` + `record_action` + rolling-24h query chạy đúng contract §3/§5: chỉ `outcome='success'` tính `success`, mọi row trong 24h tính `attempts`, `by_class` group per action_class.

**Files changed:**
- `agent_reach/runtime/farm.py` (+104): `_SCHEMA` → tuple 3 statement (accounts + actions_log FK + index `actions_log_account_ts(account,ts)`), `PRAGMA busy_timeout=5000` trong `connect()`, `_with_busy_retry` (3 retry → UPSTREAM_BROKEN), `_require_account` (NEED_SETUP), `record_action`, `ledger_24h`.
- `agent_reach/runtime/cli.py` (+10/-7): nhánh `ns.sub == "ledger"` → `_mgmt("farm account ledger", farm.ledger_24h, ns.name)`, usage guard mirror `env`, HELP + valid-ops cập nhật.
- `tests/test_farm.py` (+149): 14 test mới (22 case kể parametrize) phủ hết I/O matrix.
- Plan file này (story-farm-ledger-plan.md).

**Review:** quick pass 1 → 1 finding `medium` patched (`ledger_24h` thiếu busy-retry → wrap `_with_busy_retry`, thêm param `what`). 0 deferred, 0 rejected. followup_review_recommended=false.

**Verification:** `pytest tests -x -q` → 860 passed (838 baseline + 22) sau patch. Live e2e `/private/tmp/farm-115-e2e`: add `e2e` → 7 record_action (post 3s+2f, like 1s+1PQD) → `farm account ledger e2e` = `success=4, attempts=7, by_class{post:3/5, like:1/2}`; `ledger ghost` → NEED_SETUP exit 1.

**Residual:** `list_accounts`/`account_env` (pre-existing) cùng exposure raw OperationalError khi DB locked — contract §3 chỉ bắt writer; không trong scope diff này. WAL multi-writer test thực (lock contention) bỏ qua — chỉ assert `PRAGMA busy_timeout=5000`.
