---
title: 'x reply + x quote'
type: feature
ticket: '5'
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

**Problem:** `agent-reach twitter reply <tweet-url> "text"` and `agent-reach twitter quote <tweet-url> "text"` — same CreateTweet family as `post` (story 12.4), only `variables` differ.

**Approach:** Reply adds `variables.reply = {in_reply_to_tweet_id, exclude_reply_user_ids: []}`; quote adds `variables.attachment_url = <canonical x.com status URL>`. Both replay the SAME envelope (query_id, path, headers, features), so the extra variables are merged into the in-memory `body_template` deep-copy inside `_twitter_write.py` — no second/third skeleton files (simplest correct; documented as the skeleton decision). `twitter_post` refactors into `_send_create_tweet(extra_vars, label, verb, key_prefix, extra_raw)`; `twitter_reply`/`twitter_quote` validate the target (`_tweet_id`: raw digits or `/status(es)?/<id>` inside an x.com/twitter.com URL) BEFORE any browser work — bad/missing id → BAD_INPUT.

## Boundaries & Constraints

**Always:** Same fire-and-poll pattern; per-call `window` result key (`__xReplyResult_`, `__xQuoteResult_`); `__X_TEXT` still substituted LAST; live tokens override skeleton headers every send; BAD_INPUT before `x_write_target`; idempotent post text checks unchanged.

**Never:** No new skeleton files; no `farm.record_action` wiring (story 12.7 owns that seam, running in parallel); no thread (12.6) or media support; read actions untouched.

**Ask first:** —

## Decisions

- No `_x_reply_vars.json`/`_x_quote_vars.json` — the captured CreateTweet envelope is shared verbatim; reply/quote differ only in `variables`, injected at send time. If a future X build needs different features/headers per verb, skeletons can be split then.
- Quote `attachment_url` canonicalizes to `https://x.com/<user>/status/<id>` (drops tracking params, `/photo/N` suffixes, normalizes twitter.com → x.com). Raw numeric id (no author known) falls back to `https://x.com/i/web/status/<id>` — X's resolver route; verified acceptable pattern-wise but only live-tested via the `<user>/status` form.
- Param name `url` (not `target`) — matches the facebook write actions' convention (`_comment`, `_share`, `_post_edit`).

## Verification Plan

- Unit: mirror `test_twitter_write.py` post harness — reply success carries `in_reply_to_tweet_id`, quote carries `attachment_url`, BAD_INPUT on non-X url / X url without status id / empty text BEFORE browser (`harness["evals"] == []`), per-call keys, error mapping (429→RATE_LIMITED, no_tokens→NEED_LOGIN, errors[]→UPSTREAM_BROKEN), channel wiring for both actions.
- Live on `@rkchyxactionsch`: one reply + one quote on `https://x.com/rkchyxactionsch/status/2124195069494513814`; reply confirmed by `twitter thread` on the target (reply appears as its child), quote confirmed by `quoted_tweet` block in the quote tweet's thread read.
- BAD_INPUT live: `twitter reply https://example.com/nope "x"` and `twitter quote https://x.com/home "x"` fail fast.

</intent-contract>

## Auto Run Result

- **Intent**: `twitter reply`/`twitter quote` — CreateTweet variables family on top of story 12.4's replay path.
- **Result**: Both verbs built and verified live on `@rkchyxactionsch`.
- **Files**: `agent_reach/channels/_twitter_write.py` (`twitter_post` → `_send_create_tweet` shared core + `_tweet_id` + `twitter_reply` + `twitter_quote`; `import copy`, `import re`); `agent_reach/channels/twitter.py` (+`reply`/`quote` Actions + `_reply`/`_quote`); `tests/test_twitter_write.py` (+16 tests, POLL_RE widened to the 3 key prefixes). No new skeleton files — `variables` mutated in-memory (documented decision).
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **950 pass** (934 + 16). Live BAD_INPUT: `reply https://example.com/nope` → BAD_INPUT 28ms, `quote https://x.com/home` → BAD_INPUT 12ms — both before browser work. Live reply: `twitter reply <target> "test 12.5 reply 🐦"` → ok, tweetId `2124197986981101957`, url `https://x.com/rkchyxactionsch/status/2124197986981101957`, `inReplyTo` `2124195069494513814` — `twitter thread 2124195069494513814` lists it as the reply. Live quote: `twitter quote <target> "test 12.5 quote 🐦"` → ok, tweetId `2124198021617631334`, url `https://x.com/rkchyxactionsch/status/2124198021617631334` — thread read shows `quoted_tweet.id = 2124195069494513814` (card rendered).
- **Deviations**: `variables` injected in Python rather than separate skeletons (same envelope); param named `url` for fb-write-action consistency; raw-id quote falls back to `i/web/status` attachment (untested shape — only `<user>/status` verified live); `screenName` still null on this build → `i/web/status` fallback URL retained.
- **Residual risks**: reply/quote variable field names assumed from X's stable CreateTweet shape — confirmed live on this build, but a future build could rename them (symptom: reply lands as a plain tweet / quote without card; re-capture via `scripts/capture_mutation.py --platform x`); `exclude_reply_user_ids` always empty — multi-participant threads may mention more users than a human reply would; farm `record_action` seam intentionally unwired (story 12.7).
