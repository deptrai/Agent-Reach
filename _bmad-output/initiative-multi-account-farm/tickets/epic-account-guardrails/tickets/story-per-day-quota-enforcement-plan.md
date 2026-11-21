---
title: 'Per-day quota enforcement'
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
followup_review_recommended: false
context: []
warnings: []
deferred:
  - summary: >-
      Twitter `thread` is dual-mode: 1 text reads a thread, >=2 posts one.
      WRITE_ACTIONS treats every `thread` run as write-class, so a thread
      read under REACH_CAP_THREAD is refused and (since 12.7) counted.
    evidence: |-
      agent_reach/channels/twitter.py `_thread` routes len(texts)==1 to
      `_opencli_thread` (read). The action name cannot distinguish modes, so
      the 12.7 ledger seam records thread-reads as class "thread" — a
      pre-existing conflation this story's cap check now surfaces when an
      operator sets REACH_CAP_THREAD. No default cap on thread, so the
      exposure is opt-in only. Proper fix splits read-mode at the 12.7 seam.
    location: >-
      agent_reach/runtime/dispatch.py WRITE_ACTIONS["twitter"];
      agent_reach/channels/twitter.py:126
    severity: low
  - summary: >-
      Cap enforcement is check-then-act, not atomic — two processes sharing
      REACH_ACCOUNT can both pass the check and overshoot the cap.
    evidence: |-
      `_enforce_write_quota` reads ledger_24h, the backend runs, then
      `_record_farm_write` inserts the success row; no reservation. In-process
      runs are serialized by _ACCOUNT_ENV_LOCK for the whole window, so the
      hole is cross-process only. The ticket itself prescribes this shape
      (soft-check); a reservation protocol is a ledger-contract change, not a
      dispatch patch. Farm runners that serialize per-account invocations are
      unaffected.
    location: >-
      agent_reach/runtime/dispatch.py:_enforce_write_quota
    severity: low
---

## Intent

Dispatch refuses a write verb **before any backend call** when that account's rolling-24h **success** count for the verb's `action_class` has reached the per-class cap. Caps are `REACH_CAP_<CLASS>` (hyphen → underscore, uppercased) overriding code defaults. Reads, verbs outside `WRITE_ACTIONS`, and the legacy path (`REACH_ACCOUNT` unset) are uncapped. A refusal surfaces as a JSON envelope and writes **no** ledger row. Ledger unreadability refuses the write (fail-closed).

## Code Map

- `agent_reach/runtime/dispatch.py` — `_run_action` enters the env window (`_farm_env_enter`, L365+) then runs backends. `run_action` always calls `_record_farm_write` on the returned envelope, including failures. `WRITE_ACTIONS` maps channel → write verbs; the verb name **is** the ledger `action_class`. `RATE_LIMITED` is special: `_remember` pauses the channel for `RATE_LIMIT_SECONDS` (3600s) and `BACKEND_PAUSE_SECONDS` pauses backends — a quota refusal must not use that code.
- `agent_reach/runtime/farm.py` — `ledger_24h(name)` returns `by_class[class].success` (only `outcome='success'` counts). Raises `ReachError` (`NEED_SETUP` / `BAD_INPUT` / `UPSTREAM_BROKEN`).
- `agent_reach/runtime/result.py` — `FINAL_CODES` stops backend fallback. New code `QUOTA_EXCEEDED` joins `FINAL_CODES` and `DEFAULT_HINTS`. It is **not** in `BACKEND_PAUSE_SECONDS` and is not `RATE_LIMITED`, so `_remember` does not pause.
- `docs/farm-account-contract.md` §3 — only success counts toward quota; attempts are separate. A pre-platform refusal made no attempt. §6 still lists cap enforcement as deferred; story 13.4 owns that doc update.
- Tests: `tests/test_farm_ledger_wiring.py` (`_WriteProbe` borrows `facebook` so `WRITE_ACTIONS` applies). `tests/conftest.py` isolates `HOME` per test.

## Tasks & Acceptance

- [ ] `result.py`: add `QUOTA_EXCEEDED` + hint; include it in `FINAL_CODES`.
- [ ] `dispatch.py`: `DEFAULT_CAPS` + `_cap_for` + `_enforce_write_quota`. Call it inside `_run_action` immediately after `_farm_env_enter` succeeds, before `cooldown` / public-web / backends. Skip when no account, verb not in `WRITE_ACTIONS`, or cap is `None` (unlimited — do not query the ledger). Refuse when `success >= cap` with a message containing `cap`, the account, the class, and the current count. `ledger_24h` `ReachError` propagates; any other failure becomes `UPSTREAM_BROKEN` (fail-closed). Invalid `REACH_CAP_*` (non-integer or negative) is `BAD_INPUT`.
- [ ] `_record_farm_write` skips `QUOTA_EXCEEDED` so a refusal adds no row. Other outcomes stay recorded.
- [ ] `tests/test_farm_quota.py`: over-cap refuse, under-cap pass, unset `REACH_ACCOUNT` pass-through, non-write verb pass-through, env override, refusal adds no row, ledger failure is `UPSTREAM_BROKEN` and the backend does not run.
- [ ] `uv run --extra dev python -m pytest tests -q --tb=no` — baseline 966 still green.

### Verify

`REACH_ACCOUNT=a1` + `REACH_CAP_POST=2`: the third `post` returns `ok: false`, `error.code == QUOTA_EXCEEDED`, message contains `cap`; `ledger_24h` still shows 2 successes and 2 attempts.

## Implementation Notes

- Defaults (per class, success/24h): `post=30`, `reply=60`, `comment=60`, `react=200`, `share=20`. Every other write class (`quote`, `thread`, `post-edit`, `post-delete`, `unreact`, `uncomment`, `message`) is unlimited unless `REACH_CAP_<CLASS>` is set. `0` means always refuse.
- Env key: `REACH_CAP_` + `action_name.upper().replace("-", "_")` so `post-edit` → `REACH_CAP_POST_EDIT`.
- **Why not `RATE_LIMITED`**: `_remember` treats it as a platform rate limit and sets a 3600s channel cooldown plus the "Reach pauses this channel" hint. A farm cap is an operator policy, final for this call only, and must not pause the channel or other accounts' backends. `BAD_INPUT` would mis-label a healthy request.
- **Why skip recording**: contract §3 counts attempts the platform actually received. The verify line requires the ledger to stay at 2 success rows. Recording the refusal would also inflate `attempts` and, after 24h of refusals, is noise. `UPSTREAM_BROKEN` from an unreadable ledger still goes through the normal record seam; that write fails too and is already swallowed, so the action stays refused.
- Quota is checked even when the caller passes `only_backend` — the cap is about the account, not the backend.

## Plan Change Log

- 2026-11-22: created (route pinned `full`, risk medium from the ticket).
- 2026-11-22: review patch — gate refusals are flagged via `meta.quota_refused`
  on the envelope and the record seam skips the flag (was: skip only
  `QUOTA_EXCEEDED`), so an unreadable-ledger or bad-cap refusal also leaves no
  row. Resolves the phantom-attempt finding for partial registry failures.

## Review Triage Log

### 2026-11-22 — Review pass
- verdicts: 4 findings — high 0, medium 0, low 3, false 0, maybe-false 0
- findings:
  - `[low]` `[defer]` twitter `thread` is dual-mode; a thread *read* can be refused by REACH_CAP_THREAD and counted as class "thread" — verified at `_thread` (twitter.py:126): len(texts)==1 reads via `_opencli_thread`. Class conflation predates this diff (12.7 seam); ticket pins enforcement to the WRITE_ACTIONS map, and no default caps `thread`, so exposure is operator opt-in → deferred.
  - `[low]` `[defer]` cap check-then-act is not atomic; cross-process concurrent writes can overshoot — verified: read in `_enforce_write_quota`, insert later in `_record_farm_write`, no reservation; in-process serialized by `_ACCOUNT_ENV_LOCK`. Spec itself prescribes soft-check semantics; fixing needs a ledger-contract reservation protocol → deferred.
  - `[low]` `[patch]` gate infrastructure failure (ledger read broken, record write still works) recorded a phantom attempt row — verified by reasoning + test shape: only `QUOTA_EXCEEDED` was skipped, UPSTREAM_BROKEN/NEED_SETUP/BAD_INPUT gate refusals still recorded. Fixed: envelope flagged `meta.quota_refused`, record seam skips the flag for every gate refusal.
  - `[low]` `[patch]` llms.txt error-code list missing QUOTA_EXCEEDED (and stale PLATFORM_BLOCKED/PERSISTED_QUERY_DEAD/BUSY) — fixed: list updated in place.

## Design Notes

Code choice and fail-closed ledger behavior are the two decisions an operator would notice; both are pinned above rather than left for review to rediscover.

## Verification

```bash
uv run --extra dev python -m pytest tests/test_farm_quota.py tests/test_farm_ledger_wiring.py tests/test_farm_dispatch.py -q --tb=short
uv run --extra dev python -m pytest tests -q --tb=no
```

## Auto Run Result

**Summary** — Write verbs under `REACH_ACCOUNT` are refused before any backend when the account's rolling-24h success count for that `action_class` hits its cap. Caps come from `REACH_CAP_<CLASS>` (`post-edit` → `REACH_CAP_POST_EDIT`) else `DEFAULT_CAPS`; reads, non-map verbs, the legacy path, and uncapped classes skip the ledger entirely. Refusals surface as envelope errors with `meta.quota_refused` and never write a ledger row; an unreadable ledger refuses the write (fail-closed → `UPSTREAM_BROKEN`, gate `ReachError`s pass through).

**Refusal code**: new `QUOTA_EXCEEDED` in `FINAL_CODES` + `DEFAULT_HINTS`. `RATE_LIMITED` was rejected because `_remember` turns it into a 3600s channel cooldown and `BACKEND_PAUSE_SECONDS` pauses backends — a farm cap is per-account policy, not platform backpressure.

**Files changed**
- `agent_reach/runtime/result.py` — `QUOTA_EXCEEDED` code, hint, `FINAL_CODES` membership.
- `agent_reach/runtime/dispatch.py` — `DEFAULT_CAPS`, `_cap_for`, `_enforce_write_quota`, gate call inside the env window, `quota_refused` flag, record-seam skip.
- `tests/test_farm_quota.py` — 10 tests covering every acceptance item.
- `llms.txt` — error-code list completed.

**Review findings** — 4 findings: 2 patched (`quota_refused` flag → no phantom rows; `llms.txt` codes), 2 deferred (twitter `thread` dual-mode conflation; non-atomic check-then-act — both `low`, see `deferred`). None rejected. `followup_review_recommended: false` — no `high`/`medium` patches.

**Verification performed** — `pytest tests/test_farm_quota.py tests/test_farm_ledger_wiring.py tests/test_farm_dispatch.py -q` → 28 passed (twice: pre- and post-patch). `pytest tests -q --tb=no` → 986 passed (baseline 966 + 10 new + 10 from concurrently-landed `test_xactions_bridge.py`, not touched by this change).

**Residual risks**
- Cross-process cap overshoot (deferred finding) — farm runners should serialize per-account invocations.
- `REACH_CAP_THREAD` also gates thread *reads* until the 12.7 seam splits read-mode (deferred finding).
