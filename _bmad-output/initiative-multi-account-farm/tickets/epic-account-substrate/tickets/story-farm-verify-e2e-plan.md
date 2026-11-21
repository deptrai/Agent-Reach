---
title: 'Farm verify e2e'
type: feature
ticket: '8'
created: '2026-11-21'
status: built
baseline_revision: f684e352510ebd185221fedcf37a51537a68f6f2
route: oneshot
route_source: pinned
risk: low
review: none
review_source: pinned
lenses_ran: []
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/farm-account-contract.md'
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Stories 11.1–11.7 built the account substrate piecemeal —
registry (`add_account`/`list_accounts`), env resolve (`account_env`),
dispatch env injection (`_farm_env_enter`), ledger
(`record_action`/`ledger_24h`) — each verified in isolation. Nothing yet
proves the chain works end to end: two accounts on two HOMEs run
sequentially and each run's env, state and ledger stay on its own account.

**Approach:** Test-only story — new `tests/test_farm_e2e.py` reusing the
`_Probe` channel + `isolated_home` monkeypatch patterns from
`tests/test_farm_dispatch.py` / `tests/test_farm.py`. A probe channel records
the env and `state.root()` its backend actually observed inside the worker
thread; assertions then cover env resolve, state placement and ledger counts
per account.

## Boundaries & Constraints

**Always:**
- Test-only: no production code changes, no new deps; stdlib + pytest only.
- Reuse existing harness: `isolated_home` autouse fixture (conftest) supplies
  the outer fake HOME; `REACH_ACCOUNT` set via `monkeypatch.setenv`.
- Matrix per ticket: `add_account` a1 (`http://p1:8080`) + a2
  (`socks5://p2:9050`) → `account_env` distinct + unknown → NEED_SETUP →
  sequential `run_action` under each → `state.json` under each account's own
  `.agent-reach`, outer HOME untouched → `record_action`/`ledger_24h` counts
  per account only → `REACH_ACCOUNT=ghost` → NEED_SETUP, backend never runs.

**Never:**
- No live network/proxy traffic — probe channel is in-process.
- No re-testing of validation/CLI surface already covered by test_farm.py /
  test_farm_dispatch.py beyond what the e2e chain needs.

## Design Notes

Ledger isolation is asserted at the API level (`ledger_24h` per account) plus
one raw `GROUP BY account` count — the registry lives in the outer HOME and
is the single shared store; per-account reads are the contract boundary. A
run's own HOME tree provably carries no `farm/` subdir, so an account can't
see a registry — or the other account's ledger — from inside its run.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — baseline 869 + new tests.

## Plan Change Log

## Review Triage Log

## Auto Run Result

**Outcome:** `tests/test_farm_e2e.py` (4 test, all xanh) chứng minh cả chuỗi
substrate: `add_account` a1+a2 → `account_env` trả HOME/proxy tách biệt →
`run_action` tuần tự ghi `state.json` vào đúng `.agent-reach` của từng
account (outer HOME chỉ giữ `farm/registry.db`, không có `state.json`; không
account nào thấy `farm/` trong HOME mình) → `record_action`/`ledger_24h` đếm
đúng per account (a1: 3s/4a, a2: 1s/2a; raw `GROUP BY` = {a1:4, a2:2}) →
`REACH_ACCOUNT=ghost` → NEED_SETUP, backend không bao giờ chạy, HOME nguyên
vẹn.

**Files changed:**
- `tests/test_farm_e2e.py` (+~150, file mới duy nhất): `_Probe` channel ghi
  env + `state.root()` trong worker thread; 4 test — env resolve distinct +
  ghost NEED_SETUP; dispatch tuần tự state đúng HOME + outer untouched;
  ledger per-account + raw row counts + ghost NEED_SETUP; dispatch ghost →
  NEED_SETUP không silent fallback.
- Plan file này.

**Review:** none (route oneshot, risk low — test-only). 0 deferred, 0 rejected.

**Verification:** `pytest tests/test_farm_e2e.py` → 4 passed; full suite
`pytest tests -x -q` → 873 passed (869 baseline + 4).

**Residual:** Isolation chứng minh ở tầng env/state/ledger trong-process
(`_ACCOUNT_ENV_LOCK` serialize). Layer-1 egress soft-check và netns
hard-enforce (contract §2) vẫn deferred đúng epic-account-guardrails /
epic-fleet-orchestration — test này không chứng minh zero-egress-outside-proxy.
