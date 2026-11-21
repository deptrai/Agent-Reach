---
title: 'x thread (write: reply-chain posting)'
type: feature
ticket: '6'
created: '2026-11-22'
status: built
baseline_revision: '6b98209da84c7ba093b5b58f51385e704ab14fce'
route: full
route_source: auto
risk: medium
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** `agent-reach twitter thread "t1" "t2" ... "tN"` — post N≥2 texts as a reply-chain thread, reusing the reply machinery from 12.5 (`CreateTweet` + `variables.reply.in_reply_to_tweet_id`). Media was conditional: supported only if the upload flow could be captured as a single mutation — it could not (see Descope).

**Approach:** `twitter_thread(texts)` validates every text up-front (≥2, each non-empty ≤280 chars — BAD_INPUT before any browser work), warms the X target ONCE via `x_write_target`, then loops `_send_create_tweet` under ONE shared deadline: leg 1 posts plain, legs 2..N reply to the previous leg's `tweetId`. A failure stops the chain immediately and the raised error carries the failed index plus the ids already posted — a partial chain is never reported as success. `_send_create_tweet` gains private `_deadline`/`_tid` kwargs so legs reuse the warmed tab without re-running prepare/target/wait.

**Param design (name collision):** `find_action` returns the first action named `thread`, and the existing `thread` action is the READ ("a tweet with its replies"). The ticket requires BOTH `thread <url>` (read) and `thread "t1" "t2" ...` (write). Resolution: one merged `thread` action whose positional param becomes `Param("texts", type=list)` — a new param type. cli.py plumbs `type is list` → `nargs="+"` (values kept as a list, unlike `str` which joins with a space); `_usage` prints `<texts> [<texts> ...]`. The single backend `_thread` branches: exactly one positional that resolves through `_tweet_id` → `_opencli_thread` (read); ≥2 texts → `twitter_thread` (write); one non-URL text → BAD_INPUT naming both usages. Ledger: `WRITE_ACTIONS["twitter"]` already lists `thread` (seeded by 12.7) → write runs record automatically.

## Boundaries & Constraints

**Always:** BAD_INPUT before browser work; stop-at-first-failure with index + posted ids; one shared deadline across legs so the chain can never outlive the dispatch budget (guard: <3s left → TIMEOUT before firing the next leg); per-leg `window` result keys (`__xPostResult_` leg 1, `__xReplyResult_` legs 2+); result items = one per posted tweet (`title`/`url`/`raw.tweetId`, replies carry `raw.inReplyTo`) — same shape as post/reply items.

**Never:** No media (descoped — see below); no `dispatch.py`/`farm.py` edits; no second `thread`-named action; no changes to `twitter_reply`'s public contract; no new skeleton files or query ids (thread reuses CreateTweet).

**Ask first:** —

## Descope — media (sanctioned by ticket)

Attempted ONCE per ticket: read `scripts/capture_mutation.py --platform x` — its X interceptor hooks `window.fetch`/`XMLHttpRequest` and records ONLY requests whose URL contains `/i/api/graphql/` (`const M="/i/api/graphql/"`), and only when `typeof body === "string"`. X's media upload is structurally outside that net: it runs on `upload.twitter.com/i/media/upload.json` as a multi-request **INIT → APPEND (chunked binary FormData) → FINALIZE → STATUS** flow — a different host/path, non-GraphQL, with binary bodies the hook drops. Even a captured skeleton could not be replayed by this lane, whose send envelope is exactly one GraphQL POST per tweet (`_send_create_tweet`); `variables.media.media_entities` would need `media_id`s produced by that separate upload pipeline. → **Thread is text-only**; noted in the Action help text ("text only, no media") and here. Re-capture/revisit only if a dedicated upload-replay lane is ever built.

## Decisions

- Merged read+write under one `thread` action rather than inventing a second verb (`thread-post`/`tweetstorm`): keeps the ticket's exact CLI surface and the seeded ledger verb `thread`.
- `type=list` param instead of `action="append"` flags: `x thread "a" "b" "c"` positionals match the ticket verbatim; plumbing is 2 lines in cli.py.
- Shared deadline + pre-fired <3s TIMEOUT guard instead of per-leg fresh budgets: a fresh 30s budget per leg could outlive the dispatch timeout and strand an in-flight post.
- Side effect accepted: under `REACH_ACCOUNT`, a `thread` READ also appends an `actions_log` row (verb names collide by design — noted in `WRITE_ACTIONS` comment).
- Edge accepted: direct (non-CLI) `run_action` callers passing `texts` as a bare str hit `_validate`'s `list(value)` coercion ("abc" → ['a','b','c']). CLI can never produce a bare str (`nargs="+"` always yields a list).

## Verification Plan

- Unit (mirror existing harness): chain order (`in_reply_to_tweet_id` = previous leg's id, `__xPostResult_` → `__xReplyResult_` key prefixes, `inReplyTo` in raw); BAD_INPUT <2 texts / empty / non-str / >280 with zero evals; failure at leg 2 → message carries `tweet 2/3` + posted id + underlying error, leg 3 never sent; failure at leg 1 → `tweet 1/N` wrap; tiny timeout → TIMEOUT before first send; channel wiring + `_thread` branch (read/write/BAD_INPUT).
- Live on `@rkchyxactionsch`: post `thread "thread test 1/3 🧵" "2/3" "3/3"`, then read `twitter thread <first-url>` and confirm the chain surfaces in order with `in_reply_to` linkage.

</intent-contract>

## Auto Run Result

- **Intent**: `agent-reach twitter thread "t1".."tN"` — reply-chain posting on the 12.5 reply machinery; `thread <url>` read preserved under the same verb.
- **Result**: Built and verified live on `@rkchyxactionsch`.
- **Files**: `agent_reach/channels/_twitter_write.py` (+`twitter_thread`, `_thread_posted_suffix`; `_send_create_tweet` gains private `_deadline`/`_tid`; `TIMEOUT` imported); `agent_reach/channels/twitter.py` (`thread` action → merged read/write, `Param("texts", type=list)`, `timeout=90`; +`_thread` branch method); `agent_reach/runtime/cli.py` (`type is list` → `nargs="+"` without the str-join, `<texts> [<texts> ...]` usage); `tests/test_twitter_write.py` (+7 thread tests).
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **966 pass** (959 + 7). Live post: `twitter thread "thread test 1/3 🧵" "2/3" "3/3"` → ok in 2.3s, ids `2124200685290057748` → `2124200688418955293` → `2124200691468296216` (each leg's `inReplyTo` = previous id). Live read-back: `twitter thread https://x.com/i/web/status/2124200685290057748` → 3 items in order, `in_reply_to` set on legs 2/3; canonical first URL `https://x.com/rkchyxactionsch/status/2124200685290057748`.
- **Media descope verdict**: text-only, sanctioned — evidence above (capture hook only sees `/i/api/graphql/` string bodies; upload is `upload.twitter.com` INIT/APPEND/FINALIZE binary, outside the replay envelope). Documented in Action help.
- **Deviations**: `screenName` still null on this build → items carry `i/web/status` fallback URLs (resolves canonically, verified by the read-back); a `thread` READ under `REACH_ACCOUNT` records a ledger row (verb collision, accepted); `timeout=90` default covers ~10-leg threads, `--timeout` still overrides.
- **Residual risks**: non-CLI callers passing `texts` as a bare str get `list(value)` char-coercion (CLI cannot produce it); X could rename `reply.in_reply_to_tweet_id` (legs 2+ would land as standalone tweets — same detection/re-capture path as 12.5); very long threads may still hit the 90s cap on a slow page.
