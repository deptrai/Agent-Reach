---
title: 'Dispatch account resolve'
type: feature
ticket: '4'
created: '2026-11-21'
status: built
baseline_revision: e1a70c5201a65b81c054736f8ed87bb9a49f1ebb
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

**Problem:** `run_action` ignores `REACH_ACCOUNT` — a farm run would execute under the
process default HOME, so sessions/state land in the wrong tree (epic R4, contract §1).

**Approach:** `dispatch._farm_env_enter()` resolves `farm.account_env(REACH_ACCOUNT)`
and applies the env dict to `os.environ` for the whole run region; a `finally` restores
the prior values. Unset → legacy path untouched.

**Contract (decided):**
- Env scope covers everything HOME-dependent in the run: `cooldown`, `backend_pause`,
  `_remember`, `_pace_browser`, public-web attempt, site backends — i.e. entered before
  the first `cooldown()` inside `run_action`'s main `try`, released in its `finally`.
- Unknown account → `NEED_SETUP` envelope (hint points at `farm account add`); malformed
  name → `BAD_INPUT`; both raised before any env mutation — never silent default HOME.
- Absent keys are dropped, not inherited: a registered account whose row has no proxy
  runs with `REACH_BROWSER_PROXY` **unset**, even if the caller exported one — an
  inherited proxy is a different egress identity (kill-switch §2 spirit).
- Serialization (decided): `os.environ` is process-global, so `_ACCOUNT_ENV_LOCK`
  (module `threading.Lock`) serializes the env-scoped region for account runs in-process;
  a second run waits instead of observing another account's HOME. Cross-process runs have
  private `os.environ` and stay parallel; `query_slot`/`wait_lock` file locks still apply
  inside the scope. Stronger than the ticket's slot-lock minimum (that lock only exists
  for browser actions; cooldown/state writes also need the account HOME).
- Known residual: a backend thread orphaned by `_call_with_deadline` TIMEOUT keeps
  running after env restore and may resolve paths against the restored HOME — same
  hazard class as any timed-out backend still doing I/O; documented, not fixed here.
- Out of scope: `--account` CLI flag (env var is the contract, §1), ledger writes (11.5),
  kill-switch probe (guardrails epic).

</intent-contract>

## Implementation Notes

Oneshot: ~45 LOC `dispatch.py` (`_farm_env_enter` + enter/restore wiring) + ~110 LOC
`tests/test_farm_dispatch.py` ≈ 155 LOC. Files touched: `agent_reach/runtime/dispatch.py`,
new `tests/test_farm_dispatch.py`. `farm.py` imported lazily inside the helper (read-only
use of `account_env`), keeping farm out of the hot path for legacy runs.

- `dispatch.py`: `import os`; `_ACCOUNT_ENV_LOCK = threading.Lock()`;
  `_farm_env_enter()` → resolve → acquire lock → snapshot 3 keys (`HOME`,
  `REACH_BROWSER_PROXY`, `REACH_ACCOUNT`) → apply dict (absent ⇒ `pop`) → return
  `restore()` closure (re-sets / re-pops saved values, then releases lock).
  Unset `REACH_ACCOUNT` returns a no-op restore — zero behavioral change.
- `run_action`: `release_env = _farm_env_enter()` in its own `try/except ReachError`
  → early `envelope(error=...)`; `finally: release_env()` added to the existing `try`.
- Tests (probe `Channel` stub recording `HOME`/`REACH_BROWSER_PROXY`/`REACH_ACCOUNT`/
  `state.root()` inside its `_read` backend): registered → HOME_i/state root/proxy
  applied; unknown → NEED_SETUP + backend never ran + HOME untouched; unset → legacy;
  restored after success and after backend failure; account without proxy drops
  inherited var; two accounts sequential runs isolated.

## Plan Change Log

- Env-lock acquire made bounded (`timeout=QUEUE_WAIT` → `BUSY`) instead of an
  unbounded wait — an embedder's second in-process account run fails loud after
  the same 120s budget slot locks use, never hangs silently.

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

- verdicts: 3 findings — high 0, medium 0, low 3, false 0, maybe-false 0
- findings:
  - `[low]` `[patch]` leftover `_ACCOUNT_ENV_LOCK.acquire()` after the bounded
    acquire → non-reentrant lock would deadlock instantly — fixed during edit.
  - `[low]` `[patch]` `test_env_restored_after_backend_failure` asserted
    `REACH_BROWSER_PROXY` absent without clearing a possibly-inherited var —
    fixed: `monkeypatch.delenv` first.
  - `[low]` `[note]` non-`ReachError` raise inside `_farm_env_enter` (e.g.
    sqlite failure) propagates instead of an envelope — matches the existing
    outer-scope failure semantics of `run_action`; accepted, no change.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — expected: 834 baseline + new dispatch tests pass
- `HOME=/tmp/farmdisp114 uv run agent-reach farm account add test1 --proxy http://p1:8080` then a read action with `REACH_ACCOUNT=test1` — state writes land under the account HOME tree; `REACH_ACCOUNT=kotontai` → NEED_SETUP.

## Auto Run Result

**Outcome:** `run_action` resolves `REACH_ACCOUNT` qua `farm.account_env` và áp
`{HOME, REACH_BROWSER_PROXY, REACH_ACCOUNT}` vào `os.environ` cho trọn vùng run
(cooldown → backends → `_remember`), restore trong `finally`. Unknown →
`NEED_SETUP` (hint `farm account add`), bad name → `BAD_INPUT`, unset → legacy
path zero-change, key absent → dropped (không inherit proxy ngoài).

**Delivered:** `dispatch.py` +`_farm_env_enter` (~45 LOC, lazy-import farm,
`_ACCOUNT_ENV_LOCK` serialize env-scoped region, acquire bounded `QUEUE_WAIT` →
`BUSY`); `tests/test_farm_dispatch.py` +8 tests (~140 LOC).

**Review:** quick pass → 3 low, patch hết (stray double-acquire deadlock, test
delenv hygiene, non-ReachError note). 0 reject/defer.
`followup_review_recommended: false`.

**Verification:** 846 tests pass (838 baseline + 8). Live e2e scratch HOME
`/private/tmp/farmdisp114`: `farm account add test1` → `REACH_ACCOUNT=kotontai
reddit read` → `NEED_SETUP` envelope trong 2ms; `REACH_ACCOUNT=test1 reddit
read` → `state.json`/`browser-locks`/tool-cache đều tạo dưới
`farm/accounts/test1/.agent-reach/` trong khi outer `.agent-reach/state.json`
không tồn tại — state rơi đúng HOME_i, không leak default identity.

**Residual:** backend thread mồ côi sau `_call_with_deadline` TIMEOUT vẫn chạy
sau env-restore, có thể resolve path theo HOME đã restore — cùng hazard class
với mọi timed-out backend đang I/O; documented. Concurrency: account runs
in-process serialize trên `_ACCOUNT_ENV_LOCK` (mạnh hơn minimum slot-lock của
ticket — vùng env còn bao gồm cooldown/state writes ngoài slot); cross-process
song song bình thường vì `os.environ` private per process. `--account` flag,
ledger (11.5), kill-switch probe (guardrails) ngoài scope.
