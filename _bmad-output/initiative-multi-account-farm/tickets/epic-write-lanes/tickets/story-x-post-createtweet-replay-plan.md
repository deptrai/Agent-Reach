---
title: 'x post — CreateTweet replay'
type: feature
ticket: '4'
created: '2026-11-22'
status: built
baseline_revision: '6d12035ac1c6a8204b67935e43d0c8658573452d'
route: full
route_source: auto
risk: medium
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** `agent-reach twitter post "text"` must post a real tweet. Spike 12.1 proved CreateTweet is REPLAYABLE: bearer via `__wr('973526').Rb()`, `x-csrf-token` from the `ct0` cookie, `x-client-transaction-id` via `__wr('265390').kc(host, path, method)` — all reachable in page context.

**Approach:** Capture one live CreateTweet via `scripts/capture_mutation.py --platform x` — aborted at the network layer (a fetch/XHR wrapper installed under the script's interceptor returns a fake 200 for `/CreateTweet`, so the mutation is RECORDED but never posted; the UI composer automation provides the real request envelope). The sanitized skeleton `{platform, operation, method, query_id, path_template, headers_template, body_template}` lives at `agent_reach/channels/_x_create_tweet_vars.json`. New module `_twitter_write.py` mirrors `_facebook_write.py`: `send_mutation` fire-and-poll, `_result_key`, live token mint in an async IIFE (webpack push-trick → `__wr`), `x_classify_result` mapping 429/88→RATE_LIMITED, 401/403/32→NEED_LOGIN, 404→PERSISTED_QUERY_DEAD, errors[]→UPSTREAM_BROKEN. `Action("post")` + `_post` on `TwitterChannel` (backend label `opencli` → dispatcher serialization). queryId via `query_ids.get("x","CreateTweet",<captured>)` — hot reload.

## Boundaries & Constraints

**Always:** Same fire-and-poll pattern as FB writes; per-call `window` result key; `__X_TEXT` substituted LAST (user text may contain other sentinels); live tokens override skeleton headers every send (`authorization`, `x-csrf-token`, `x-client-transaction-id`); `cookie:` header never sent; no real tweet posted during capture — the capture was aborted at fetch/XHR level (evidence: `__cap` record + fake `rest_id:"0"` returned to the composer).

**Never:** No reply/quote/thread (stories 12.5/12.6), no `record_action`/farm-ledger wiring (12.7 — seam only), no edits to `twitter.py` read actions, `scripts/capture_mutation.py`, `query_ids.py`, `dispatch.py`, or `_facebook_write.py`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY | logged-in x.com tab, `post "text"` | `ok:true`, item url = status URL, raw.tweetId + echo | — |
| NO_SESSION | no `ct0` cookie | `{error:"no_tokens"}` → NEED_LOGIN (x.com message) | send_mutation maps |
| BOOT_LAG | webpack not ready | `no_tokens,hasUid:true` retried ≤6×/1.5s, then UPSTREAM_BROKEN | send_mutation |
| RATE_LIMIT | HTTP 429 or errors[].code 88 | RATE_LIMITED | `x_classify_result` |
| AUTH_FAIL | HTTP 401/403 or code 32/89 | NEED_LOGIN | `x_classify_result` |
| DEAD_QID | HTTP 404 or "not found"+query/document | PERSISTED_QUERY_DEAD (final, non-pausing) | `x_classify_result` |
| BAD_INPUT | empty text / >280 raw chars | BAD_INPUT before browser work | `twitter_post` |
| SENT_NO_RESULT | fetch fired, poll expired | UPSTREAM_BROKEN + duplicates hint | `send_mutation` |
| TXID_FAIL | `kc` throws + module scan fails | `{error:"txid_unavailable:…"}` → UPSTREAM_BROKEN | send_mutation |
| NO_TWEET_REF | parsed but no tweetId | UPSTREAM_BROKEN (check before retry) | `twitter_post` |
| CONFIRMED+ERRORS | tweetId present + non-fatal errors[] | confirmed wins → success | `x_classify_result` |

</intent-contract>

## Code Map

- `agent_reach/channels/_twitter_write.py` — NEW: `load_x_skeleton`, `_SKELETON`, `prepare_twitter` (`session_reuse.prepare("twitter")`), `_is_x_url`, `_x_target`, `_wait_x_ready` (webpack chunk probe), `x_write_target`, `_X_SEND_TEMPLATE` (async IIFE: cookie ct0 → csrf, push-trick `__wr` → `Rb()` bearer with module-source scan + public-constant fallback, `kc()` txid with txid-module scan), `x_classify_result`, `twitter_post`. Imports `send_mutation`/`_eval`/`_result_key`/`_error_message` from `_facebook_write` (per-module `_eval` binding so tests stub per-module).
- `agent_reach/channels/_x_create_tweet_vars.json` — NEW: captured skeleton (query_id `l4hk8uVGvR-a8kzQ4nOhPA`, POST, auth headers as placeholders, real `{variables, features, queryId}` body).
- `agent_reach/channels/twitter.py` — `Action("post")` + `_post` delegating to `twitter_post`.
- `agent_reach/channels/query-ids.json` — seed `"x": {"CreateTweet": "l4hk8uVGvR-a8kzQ4nOhPA"}`.
- `tests/test_twitter_write.py` — NEW: 23 tests, X-shaped fake-CDP harness (mirrors `test_facebook_write.py` coverage: error matrix, sentinel order, per-call keys, NEED_LOGIN wording, qid config override, channel wiring).

## Tasks & Acceptance

**Execution:**
- [x] capture CreateTweet via abort-under-interceptor (composer UI automation; no tweet leaked)
- [x] `_x_create_tweet_vars.json` skeleton committed (sanitized)
- [x] `_twitter_write.py` + `Action("post")`/`_post` wiring
- [x] `query-ids.json` seed entry
- [x] `tests/test_twitter_write.py`
- [x] live verify: one real tweet on the designated test account

**Acceptance Criteria:**
- Given a logged-in x.com session, `agent-reach twitter post "text"` returns `ok:true` with the tweet URL and the tweet exists on the profile
- Given no ct0, the action raises NEED_LOGIN naming x.com (not facebook.com)
- Skeleton on disk carries only placeholder auth headers
- `uv run --extra dev python -m pytest tests -x -q` stays green (baseline 911)

## Implementation Notes

- `auth_token` is HttpOnly — not JS-readable; presence is implied by ct0 + `credentials:"include"`. Checking it in-page false-fails (found live).
- `screenName` extraction can be null on some builds — `twitter_post` falls back to `https://x.com/i/web/status/<id>` (canonical redirect).
- Capture-script note: its X watcher dumps `__cap` only at window end — reloading the tab (to clear interceptors) destroys `__cap`; extracting the record via `import capture_mutation` + `_skeleton_x`/`default_out_name_x` mid-window yields the identical file.

## Plan Change Log

## Review Triage Log

## Design Notes

`__wr` is not a window global — captured via `webpackChunk_twitter_responsive_web.push([[id],{},r=>{__wr=r}])` (verified live on a cold tab). Module ids 973526 (bearer `.Rb`) and 265390 (txid `.kc`) verified live; both have `__wr.m` source-scan fallbacks for module-id churn, plus the public bearer constant as last resort. txid is minted over `(host, path, method)` with the SAME path sent to fetch — they must match.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — all pass (baseline: 911)
- `uv run agent-reach twitter post "test 12.4 🐦" --timeout 90` — live post
- live existence check: navigate status URL in the dedicated tab → rendered `tweetText`

## Auto Run Result

- **Summary**: `twitter post` posts a real tweet via CreateTweet GraphQL replay in the dedicated browser — capture-once skeleton (`_x_create_tweet_vars.json`) + live token mint (`__wr` push-trick → `Rb()` bearer, `ct0` cookie → `x-csrf-token`, `kc()` txid) + shared `send_mutation` fire-and-poll. Capture used an abort-under-interceptor so NO tweet was posted during capture; the UI composer supplied the real request envelope.
- **Files**: `agent_reach/channels/_twitter_write.py` (new, ~300 lines); `agent_reach/channels/_x_create_tweet_vars.json` (new skeleton, query_id `l4hk8uVGvR-a8kzQ4nOhPA`); `agent_reach/channels/twitter.py` (+`post` Action/`_post`); `agent_reach/channels/query-ids.json` (+`x.CreateTweet`); `tests/test_twitter_write.py` (23 tests).
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **934 pass** (911 + 23). Live: `agent-reach twitter post "test 12.4 🐦"` → `ok:true`, tweetId `2124195069494513814`, url `https://x.com/i/web/status/2124195069494513814` → resolves to `https://x.com/rkchyxactionsch/status/2124195069494513814` (test account `@rkchyxactionsch`); rendered tweetText `test 12.4 🐦` (unicode preserved); 877ms. Profile scan: no leaked capture-probe tweet. Tweet is REAL — delete at x.com when convenient.
- **Deviations**: CLI verb is `twitter post` (channel slug `twitter`, no `x` alias exists); skeleton written mid-window via script's own `_skeleton_x`/`default_out_name_x` after tab reload destroyed `__cap` (identical output); `send_mutation`'s NEED_LOGIN message is FB-flavored → `twitter_post` rewords to x.com on catch; `x_classify_result` written X-flavored rather than reusing FB strings; `screenName` extraction returns null on this build → `i/web/status` fallback URL.
- **Residual risks**: `kc`/module ids rotate with X builds (source-scan + public-bearer fallbacks; re-capture skeleton on repeated PERSISTED_QUERY_DEAD); >280 weighted but ≤280 raw chars (CJK) passes BAD_INPUT then fails at X → UPSTREAM_BROKEN; farm `record_action` seam intentionally unwired (story 12.7).
