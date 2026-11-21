---
title: 'Persisted-query-dead error class'
type: feature
ticket: '6'
created: '2026-11-21'
status: built
baseline_revision: 11fd6ba3f8e9c9676f39833962132bf7b63f9049
route: oneshot
route_source: pinned
risk: low
review: none
review_source: pinned
lenses_ran: []
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** A rotated FB persisted-query `doc_id` ("The GraphQL document with ID … was not found") currently surfaces as `UPSTREAM_BROKEN` — a pausing, retry-implying code. Per `docs/farm-account-contract.md` §4 the signal is lane-terminal and needs its own error class so the farm layer can later route the account to a fallback lane.

**Approach:** Add `PERSISTED_QUERY_DEAD` as a first-class code in `agent_reach/runtime/result.py` (constant + `FINAL_CODES` + `DEFAULT_HINTS`), and detect the persisted-query signal in `classify_result` (`agent_reach/channels/_facebook_write.py`, shared by all FB mutation modules) so GraphQL `errors[]` containing the dead-document phrase raise it instead of `UPSTREAM_BROKEN`.

## Boundaries & Constraints

**Always:** R6 + ticket verify: error path with persisted-query signal → `error.code = PERSISTED_QUERY_DEAD` in the envelope; `RATE_LIMITED`/`PLATFORM_BLOCKED`/`NEED_LOGIN` semantics unchanged. Classification: **final, non-pausing** — `FINAL_CODES` member (same-lane retry prohibited; another site backend replays the same dead doc_id; cross-lane fallback is farm-level, owned by epic-farm-resilience, not dispatch's public-web mechanism) and **not** in `BACKEND_PAUSE_SECONDS` (terminal, not "retry later").

**Never:** No fallback routing implementation (epic-farm-resilience). No new dependencies. No touching `BACKEND_PAUSE_SECONDS`, cooldown, or rate-limit logic. Do not broaden detection to plain "not found" (would shadow `NOT_FOUND`/ambiguous upstream errors).

</intent-contract>

## Tasks & Acceptance

**Execution:**
- [ ] `agent_reach/runtime/result.py` — `PERSISTED_QUERY_DEAD` constant, `FINAL_CODES` membership, `DEFAULT_HINTS` entry ("stale doc_id — re-capture the mutation skeleton; do not retry this replay").
- [ ] `agent_reach/channels/_facebook_write.py` — `classify_result` raises `ReachError(PERSISTED_QUERY_DEAD, …)` when `errors[]` message carries the dead-document signal ("document" + "not found", case-insensitive — covers "The GraphQL document with ID … was not found").
- [ ] `tests/test_facebook_write.py` — PQD classification test via the post path; code set/envelope assertions.
- [ ] `tests/test_dispatch_public_fallback.py` — add `PERSISTED_QUERY_DEAD` to the final-codes-never-fall-back parametrize.

**Acceptance Criteria:**
- Given a GraphQL `errors[]` payload saying the document was not found, when `classify_result` runs, then `ReachError.code == PERSISTED_QUERY_DEAD` and `envelope()` renders `ok:false` + `error.code = PERSISTED_QUERY_DEAD` + a default hint.
- Given the code sets, when inspected, then `PERSISTED_QUERY_DEAD ∈ FINAL_CODES` and `∉ BACKEND_PAUSE_SECONDS`; `RATE_LIMITED`/`PLATFORM_BLOCKED`/`NEED_LOGIN` mappings unchanged.

## Implementation Notes

- Detection string: lowercase message contains `"document"` and `"not found"` — matches the canonical FB text and the "document not found" phrase every FB write module docstring cites ("re-capture if … failing with 'document not found'").
- Hint wording follows `DEFAULT_HINTS` style: what happened + what the agent must not do (no same-lane retry loop).
- `FINAL_CODES` is also read by dispatch's public-web fallback (`not any(e.code in FINAL_CODES …)`): PQD membership keeps a dead doc_id from triggering an unrelated public-web attempt.

## Verification

```bash
uv run --extra dev python -m pytest tests -x -q
uv run python -c "from agent_reach.runtime.result import PERSISTED_QUERY_DEAD, FINAL_CODES; assert PERSISTED_QUERY_DEAD in FINAL_CODES"
```

## Auto Run Result

- `agent_reach/runtime/result.py`: `PERSISTED_QUERY_DEAD` constant, added to `FINAL_CODES` (final per-lane), `DEFAULT_HINTS` entry added.
- `agent_reach/channels/_facebook_write.py`: `classify_result` raises `PERSISTED_QUERY_DEAD` when `errors[]` message contains "document"+"not found" — shared by all FB mutation modules.
- `tests/test_facebook_write.py`: +3 tests (classification, final/non-pausing, `ok:false` envelope + hint). `tests/test_dispatch_public_fallback.py`: `PERSISTED_QUERY_DEAD` added to final-codes parametrize.
- `pytest tests -x -q`: **828 passed** (baseline 824). No deviations.
