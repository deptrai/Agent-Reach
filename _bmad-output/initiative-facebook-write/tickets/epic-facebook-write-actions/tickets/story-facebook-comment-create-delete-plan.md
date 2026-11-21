---
title: 'facebook comment create + delete'
type: feature
ticket: '3'
created: '2026-11-21'
status: built
baseline_revision: '2e926b17ddbf3d196be126d79edf4612d34b0e4f'
route: full
route_source: auto
risk: medium
review: ''
review_source: ''
lenses_ran: [quick]
review_loop_iteration: 0
followup_review_recommended: false
context:
- agent_reach/channels/_facebook_react.py
- agent_reach/channels/_facebook_write.py
- agent_reach/channels/facebook.py
- tests/test_facebook_react.py
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** `agent-reach facebook` can post and react but cannot comment — the largest remaining write surface on a post, needed for engagement workflows.

**Approach:** Capture the two real UFI comment mutations (add + delete) once via the capture harness on a test post, store sanitized `{params, variables}` skeletons, and replay them through the same fire-and-poll pipeline already used by `post`/`react` — no new machinery, just a new mutation family.

## Boundaries & Constraints

**Always:** Reuse `_eval`, `_fb_target`, `_wait_ready`, `left(cap)` deadline, per-call uuid result key from `_facebook_write.py`; reuse `_check_url`, `_same_post_path`, `_ensure_post_loaded`, `_resolve_feedback_id` from `_facebook_react.py` (import, do not copy). Skeletons sanitized exactly like story 1.1/1.2 (`{"params":..., "variables":...}` format, actor/uid → "0", uuids → zero-uuid, epochs → 0). Typed `ReachError`: NEED_LOGIN / RATE_LIMITED (429) / UPSTREAM_BROKEN / BAD_INPUT. Fire-and-poll result pattern — never blind-retry a mutation. Fire in a facebook.com page target inside the dedicated Reach browser via `Runtime.evaluate` (`credentials:"include"`).

**Never:** No Puppeteer, no DOM click-as-shipped-path, no credential export, no commenting on behalf of other actors than the logged-in user, no changes to read actions or `post`/`react`, no blind retry after send. Do not construct `feedback_id` from post_id/comment_id arithmetic — resolve live on the page or take it from the create response.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH_CREATE | `facebook comment <post-url> "text"` | ok:true + item with comment id/url; comment visible on post | — |
| HAPPY_PATH_DELETE | `facebook uncomment <comment-permalink-or-id>` | ok:true + removed:true; comment gone on FB | — |
| BAD_URL | non-facebook URL / garbage | — | BAD_INPUT before browser work |
| MISSING_TEXT | `facebook comment <url>` no text | — | BAD_INPUT |
| NO_FEEDBACK_ID | post page has no resolvable feedback_id | — | UPSTREAM_BROKEN |
| NOT_LOGGED_IN | no c_user session in dedicated browser | — | NEED_LOGIN |
| RATE_LIMIT | FB responds HTTP 429 | — | RATE_LIMITED |
| DELETE_NOT_OWN/NOT_FOUND | delete id not found or not deletable | mutation errors / no comment id in response | UPSTREAM_BROKEN |

</intent-contract>

## Code Map

- `agent_reach/channels/_facebook_write.py` — pattern source: `_POST_JS` fire-and-poll, `_PARAMS_TEMPLATE`/`_VARS_SKELETON` from `{"params","variables"}` file, `left()` deadline, `make_item`, `_eval`/`_fb_target`/`_wait_ready` exports.
- `agent_reach/channels/_facebook_react.py` — closest sibling: `_ensure_post_loaded` (navigate + href-match poll, the race fix), `_resolve_feedback_id` (live-page feedback_id), `_check_url`, `_same_post_path`, `_send_reaction`/`_reaction_result` naming to mirror, `backends=(("opencli",...),)` action pattern.
- `agent_reach/channels/facebook.py` — add `Action("comment", ...)` (params `url`, `text`) and `Action("uncomment", ...)` (param `url` — permalink or raw id) calling `_comment`/`_uncomment`; comment the "opencli" label serializes browser writes.
- `agent_reach/channels/_fb_comment_add_vars.json`, `agent_reach/channels/_fb_comment_delete_vars.json` — NEW sanitized skeletons, one per mutation (same shape as `_fb_react_vars.json`).
- `scripts/capture_fb_comment.py` — NEW scaffold (already exists, untracked): arms `window.__fbCap` interceptor on the dedicated-browser FB tab, navigates to `POST_URL`; finish `create`/`delete` phases that trigger a real comment via the page's own composer (opencli `fill`/`eval` or CDP eval — NOT a fabricated request) then dump+sanitize via `capture_fb_mutation.py`'s `parse_body`/`sanitize`.
- `tests/test_facebook_comment.py` — NEW; mirror `test_facebook_react.py` monkeypatch harness (fake `_eval` routing SEND vs POLL vs LOCATE by regex).
- `tests/test_facebook_write.py` — reference for harness conventions (do not modify unless shared helper needed).

## Tasks & Acceptance

**Execution:**
- [ ] `scripts/capture_fb_comment.py` -- FINISH create/delete capture phases; run once against test post to produce both real bodies; sanitize → write `_fb_comment_add_vars.json` + `_fb_comment_delete_vars.json` -- skeletons are the ground truth for `doc_id` + `variables`; expected mutation names along `CometUFIAddComment*`/`CometUFIDeleteComment*` — record actual `fb_api_req_friendly_name` + `doc_id` found.
- [ ] `agent_reach/channels/_facebook_comment.py` -- NEW `facebook_comment(*, url, text, timeout)` + `facebook_uncomment(*, target, timeout)`; `_LOCATE`-style eval finds the target post's feedback_id (reuse `_resolve_feedback_id`); send JS follows `_POST_JS` template-params pattern; create result must extract comment id/permalink from response (`data.<mutation>.comment`/feedback id fields — read actual shape at capture); delete resolves comment's own feedback_id from permalink, or accepts raw id directly. Verify state in result like `_reaction_result` (created id non-null; delete reports removal), classify 429/login-shaped/errors per established mapping.
- [ ] `agent_reach/channels/facebook.py` -- register `comment` + `uncomment` actions with `(("opencli","_comment")), (("opencli","_uncomment"))` backends; validate `text` non-empty / target non-empty → BAD_INPUT.
- [ ] `tests/test_facebook_comment.py` -- NEW; cover I/O matrix: create happy path, delete by permalink + raw id, BAD_INPUT cases, NEED_LOGIN, 429→RATE_LIMITED, GraphQL errors→UPSTREAM_BROKEN, per-call result keys, sentinel substitution order.

**Acceptance Criteria:**
- Given dedicated browser logged into facebook.com, when `agent-reach facebook comment <test-post-url> "text"`, then ok:true within ~10s and the comment is present on the post with a returned id/url.
- Given the created comment's permalink/id, when `agent-reach facebook uncomment <target>`, then ok:true and the comment no longer exists on FB.
- Given `--text` missing or a non-FB url, when either action runs, then BAD_INPUT before any browser call.

## Implementation Notes

## Plan Change Log

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)
- [medium][patch] `facebook_uncomment` returns ok:true on silent no-op deletes (`{"data":{"comment_delete":null}}`, `{"data":{}}`) — never verified a positive deletion signal; plan requires `removed:true` + DELETE_NOT_OWN→UPSTREAM_BROKEN. Patch: require dataKeys has comment_delete AND echoesComment (padding-normalized), add removed:true.
- [low][patch] base64url `-`/`_` missing from `_COMMENT_ID_RE`, locate regex, `_raw_comment_id`, `_comment_permalink` — JS already normalizes but Python rejects base64url ids → BAD_INPUT on round-trip ids / locate misses. Patch: include `-_` + urlsafe decode.
- [low][patch] `scripts/capture_fb_comment.py:16` — hardcoded abs sys.path.insert → `Path(__file__).resolve().parents[1]`.
- [medium][patch] `scripts/capture_fb_comment.py` delete mode is probe stub — never opens ⋯ menu / triggers delete; `sys.argv[2]` IndexError unguarded. Patch: complete delete capture leg + argv guard (or document).
- [low][patch] `agent_reach/runtime/onboarding.py:39` — capability text omits `unreact`.
- [low][patch] `focus_expr` — `a[href*="comment_id=<cid>"]` prefix-matches `<cid>0` → wrong scroll target → poll-to-UPSTREAM_BROKEN. Patch: delimiter/end-anchored match.

## Design Notes

Comment delete needs the **comment's own** feedback_id (unlike react which used the post's). Two accepted inputs: (a) raw id string from our create response → pass through; (b) permalink URL → navigate + live-resolve the comment node's feedback_id via a `_LOCATE`-family eval (anchor on the comment's own DOM/node, never on the post's — wrong-target guard: if resolution is ambiguous, prefer failing UPSTREAM_BROKEN over deleting the wrong object).

Test post (own, only_me): `https://www.facebook.com/61554949615037/posts/122311668092164987` — safe comment/delete target; clean up test comments after e2e.

Result-key/poll/deadline plumbing is identical to `_facebook_react.py`'s `_send_reaction` — mirror its structure so review diff stays small.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` -- expected: all pass (baseline 673 + new comment tests)
- `agent-reach facebook comment "https://www.facebook.com/61554949615037/posts/122311668092164987" "bmad story 1.3 e2e 💬"` -- expected: ok:true, comment id/url in item
- `agent-reach facebook uncomment <returned-target>` -- expected: ok:true, comment gone
- `agent-reach facebook comment "https://example.com/x" "bad"` -- expected: ok:false, BAD_INPUT

**Manual checks (if no CLI):**
- Open test post in browser: comment appears after create, absent after uncomment.

## Auto Run Result

**Status: built** — `facebook comment` + `facebook uncomment` implemented, reviewed (1 pass, 6 findings → 6 patches), verified live.

### Delivered
- `agent_reach/channels/_facebook_comment.py` (481+ lines): `facebook_comment(*, url, text, timeout)`, `facebook_uncomment(*, url, timeout)`; replays `useCometUFIAddCommentMutation` + `useCometUFIDeleteCommentMutation` via fire-and-poll CDP fetch; comment-id resolve by permalink `?comment_id=` (live `_COMMENT_LOCATE_JS` base64 decode) or raw base64/base64url Relay id.
- `agent_reach/channels/_fb_comment_vars.json`: `{params, create{variables}, delete{variables}}` sanitized skeleton.
- `agent_reach/channels/facebook.py`: `comment` + `uncomment` actions.
- `scripts/capture_fb_comment.py`: full create+delete capture legs.
- `agent_reach/runtime/onboarding.py`: write capability text lists all 5 actions.
- `tests/test_facebook_comment.py`: 33 tests (full matrix + JS brace-balance sanity guard).

### Review — 6 findings, all patched
- [medium] delete no-op now fails closed: requires `comment_delete` payload + `echoesComment`/`removed:true` (padding-normalized echo compare).
- [medium] `capture_fb_comment.py` delete leg implemented (menu open → Delete → confirm → sanitize).
- [low] base64url charset, sys.path hardcode, onboarding text, focus_expr prefix-match.

### Verification
- `uv run --extra dev python -m pytest tests -x -q` → **706 passed** (baseline 673; +26 story tests +7 review tests).
- Live e2e (own only_me post): create → permalink + commentId; delete via permalink → `removed:true, echoesComment:true`; delete via raw id → ok.
- Coordinator e2e fix: `focus_expr` double-brace SyntaxError (`{{...}}` in plain string) — found by live test, fixed pre-review, guarded by brace-balance test.

### Residual risks
- `doc_id` persists until FB build rotation — re-capture via `scripts/capture_fb_comment.py`.
- `echoesComment` is a response-echo heuristic; payload-non-null fallback covers alternate shapes.
