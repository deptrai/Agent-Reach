---
title: 'Farm account env resolve'
type: feature
ticket: '3'
created: '2026-11-21'
status: built
baseline_revision: a38d68eb82b438eb0c56e628a6e9a0744897f916
route: oneshot
route_source: auto
risk: medium
review: quick
review_source: pinned
lenses_ran: [quick]
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** `farm account env <name>` is a stub returning BAD_INPUT, so dispatch has no
resolution surface to turn `REACH_ACCOUNT=<name>` into a per-run env (epic R4).

**Approach:** `farm.account_env(name)` reads the registry row and returns the env dict;
`_cmd_farm` wires `farm account env <name>` through the `_mgmt` envelope.

**Contract (decided):**
- `result` IS the env dict, flat — `{"HOME": <abs>, "REACH_BROWSER_PROXY": <proxy>, "REACH_ACCOUNT": <name>}`
  (dispatch applies it directly; mirrors ticket wording `env dict {HOME, REACH_BROWSER_PROXY, REACH_ACCOUNT}`).
- `HOME` = registry `home` column (source of truth, R1), made absolute via
  `Path(home).absolute()` — never `resolve()` (symlink-following would diverge from the
  path `add`/`list` recorded). Fallback: derived `farm_root()/accounts/<name>` if the
  column is somehow empty.
- `REACH_BROWSER_PROXY` present only when `row.proxy` is truthy — absent, not
  empty-string, when unset (schema is NOT NULL + add validates non-empty, so this only
  covers a hand-edited DB; absent is safer than an empty var for downstream consumers).
- Unknown account → `ReachError(NEED_SETUP)`, never silent default HOME (epic R4).
- Bad name → `BAD_INPUT` via existing `_validate_name`, before any DB/fs touch.
- Out of scope: `farm env`/`farm ledger` top-level stubs stay BAD_INPUT (11.5 owns ledger);
  no dispatch.py hook (R4 dispatch wiring is a later story — this story is the resolution surface).

</intent-contract>

## Implementation Notes

Oneshot: ~25 LOC `farm.account_env` + ~6 LOC `_cmd_farm` wiring + ~65 LOC tests ≈ 96 LOC
(≤100 threshold), single module seam already established by story 11.1.

- `farm.account_env(name)`: `_validate_name` → `SELECT proxy, home` (one row) →
  `NEED_SETUP` when absent → env dict `{HOME, REACH_BROWSER_PROXY?, REACH_ACCOUNT}`.
- `_cmd_farm`: new `ns.sub == "env"` branch; usage strings + HELP line updated
  (`add|list|env`, ledger still flagged "later"). Top-level `farm env`/`farm ledger`
  stubs untouched.
- Files touched: `agent_reach/runtime/farm.py`, `agent_reach/runtime/cli.py`,
  `tests/test_farm.py` (+10 tests: happy, 2-account distinct, NEED_SETUP, bad-name ×3,
  no-proxy omits var, +3 arg-validation params).

## Plan Change Log

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

- verdicts: 3 findings — high 0, medium 0, low 3, false 0, maybe-false 0
- findings:
  - `[low]` `[patch]` `test_env_two_accounts…` hardcoded `endswith("accounts/a1")` POSIX sep → fails on Windows — fixed: compare against `str(state.root()/"farm"/"accounts"/<name>)` like sibling tests.
  - `[low]` `[patch]` `test_env_row_without_proxy…` used `with farm.connect()` (commits but never closes — sqlite3 ctx manager ≠ `closing()`) → WAL handle leak — fixed: `with closing(farm.connect()) as conn, conn:`.
  - `[low]` `[patch]` `account_env` selected unused `name` column — fixed: `SELECT proxy, home`.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — expected: 824 baseline + new env tests pass
- `HOME=/tmp/farmenv113 uv run agent-reach farm account add a1 --proxy http://p1:8080 && HOME=/tmp/farmenv113 uv run agent-reach farm account env a1` — expected: ok:true, result{HOME abs, REACH_BROWSER_PROXY, REACH_ACCOUNT}
- `HOME=/tmp/farmenv113 uv run agent-reach farm account env ghost` — expected: ok:false NEED_SETUP

## Auto Run Result

**Outcome:** `farm account env <name>` resolves `{HOME (abs), REACH_BROWSER_PROXY, REACH_ACCOUNT}` từ registry — surface R4 cho dispatch; unknown → `NEED_SETUP`, bad name → `BAD_INPUT`, proxy rỗng → var absent.

**Delivered:** `farm.py` +`account_env` (~25 LOC, tái dùng `connect`/`_validate_name`); `cli.py` +6 (`env` branch, usage + HELP); `tests/test_farm.py` +10 tests.

**Review:** quick pass → 3 low findings, patch hết (Windows path sep trong test, `closing()` cho WAL connection, dead `name` column). 0 reject/defer. `followup_review_recommended: false`.

**Verification:** 834 tests pass (824 baseline + 10). Live e2e scratch HOME `/private/tmp/farmenv113`: add a1+a2 → env a1/a2 trả đúng HOME abs + proxy từng account; `env ghost` → NEED_SETUP; `env ../bad` → BAD_INPUT.

**Residual:** `farm env`/`farm ledger` top-level stubs → story 11.5; dispatch hook đọc `REACH_ACCOUNT` → story R4 dispatch (epic sau). Quyết định contract: `result` là env dict phẳng; `REACH_BROWSER_PROXY` absent (không empty-string) khi row.proxy rỗng — chỉ xảy ra khi DB bị sửa tay vì `add` validate non-empty.
