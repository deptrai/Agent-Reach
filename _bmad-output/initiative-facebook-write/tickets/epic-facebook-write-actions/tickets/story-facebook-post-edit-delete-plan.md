---
title: 'facebook post edit + delete'
type: feature
ticket: '4'
created: '2026-11-21'
status: built
baseline_revision: 'd972494f6d5c30fceb1430c05bde0d8420a7986e'
route: full
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

**Problem:** `agent-reach facebook` can create/react/comment but cannot correct or remove a post it made — a wrong post stays on the timeline forever. Epic requirement R3 demands edit + delete on own posts, with a guard against accidental deletion.

**Approach:** Capture the two real post-edit and post-delete GraphQL mutations once via the CDP capture recipe, store sanitized `{params, variables}` skeletons, and replay them through the established fire-and-poll pipeline. Delete requires an explicit confirmation flag so a bare invocation cannot destroy a post.

## Boundaries & Constraints

**Always:** Reuse `_eval`/`_fb_target`/`_wait_ready`/`left(cap)` deadline/per-call uuid result key from `_facebook_write.py` and `_check_url`/`_ensure_post_loaded` from `_facebook_react.py` — import, don't copy. Target the post by its permalink or raw story id (from the `facebook post` response); never construct ids arithmetically. Skeletons sanitized (actor/uid → "0", uuids → zero-uuid, epochs → 0). Typed `ReachError` (NEED_LOGIN/RATE_LIMITED/UPSTREAM_BROKEN/BAD_INPUT). Fire inside a facebook.com page target via `Runtime.evaluate` with `credentials:"include"`.

**Never:** No Puppeteer, no DOM-click as the shipped path, no credential export. No changes to read actions or to `post`/`react`/`comment`. No blind retry of a write whose result is unknown. Never delete without the confirmation flag — a missing/false flag is BAD_INPUT before any browser work. Never edit or delete an object that does not resolve as the caller's own post — ambiguous resolution fails UPSTREAM_BROKEN.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| EDIT | `facebook post-edit <permalink-or-story-id> "new text"` on own post | ok:true + edited text reflected in result | No error expected |
| DELETE | `facebook post-delete <permalink-or-story-id> --confirm` on own post | ok:true + removed:true; post gone on facebook.com | No error expected |
| NO_CONFIRM | delete without `--confirm` | nothing sent | BAD_INPUT before browser work |
| NOT_OWN | target resolves to someone else's post | no mutation fired | UPSTREAM_BROKEN |
| NOT_LOGGED_IN | no session | NEED_LOGIN | NEED_LOGIN |
| RATE_LIMIT | HTTP 429 | no success | RATE_LIMITED |

</intent-contract>

## Code Map

- `agent_reach/channels/_facebook_write.py` — pattern source: `_POST_JS` fire-and-poll, `{"params","variables"}` skeleton, `left()` deadline, `make_item`; exports `_eval`/`_fb_target`/`_wait_ready`.
- `agent_reach/channels/_facebook_comment.py` — closest sibling for the delete leg: `_deleted_comment()` positive-signal check (dataKeys + echoesComment, padding-normalized), `removed:true` result field, raw-id vs permalink dual input, base64url-safe id regex — mirror its structure.
- `agent_reach/channels/_facebook_react.py` — reuse `_check_url`, `_ensure_post_loaded` (navigate + href-match poll).
- `agent_reach/channels/facebook.py` — register `post-edit` (params `url`, `text`) and `post-delete` (param `url` + confirm flag) actions with `(("opencli", ...),)` backends.
- `agent_reach/channels/_fb_post_edit_vars.json`, `agent_reach/channels/_fb_post_delete_vars.json` — NEW sanitized skeletons (or one combined file if the captured params template is shared — implementer's call, record which).
- `scripts/capture_fb_post_edit.py` — NEW capture harness (mirror `scripts/capture_fb_comment.py`): arms `window.__fbCap`, triggers a real edit + delete through the page's own UI on the test post, dumps+sanitizes via `capture_fb_mutation.py` helpers.
- `tests/test_facebook_post_edit.py` — NEW; mirror `tests/test_facebook_comment.py` monkeypatch harness.

## Tasks & Acceptance

**Execution:**
- [ ] `scripts/capture_fb_post_edit.py` -- NEW capture harness; run once against the test post to capture both real bodies; sanitize → write the skeleton file(s) -- skeletons are ground truth for `doc_id` + `variables`; record actual `fb_api_req_friendly_name` + `doc_id` (expected along `CometPostEdit*`/`CometPostDelete*` — use what capture finds).
- [ ] `agent_reach/channels/_facebook_post_edit.py` -- NEW `facebook_post_edit(*, url, text, timeout)` + `facebook_post_delete(*, url, confirm, timeout)`; resolve the post's story id from permalink (live-page eval) or accept raw story id; verify edit result reflects new text and delete result carries a positive deletion signal (`removed:true`) — silent no-op responses fail UPSTREAM_BROKEN like the comment delete leg.
- [ ] `agent_reach/channels/facebook.py` -- register both actions; `post-delete` requires `confirm` flag → BAD_INPUT when absent/false, before any browser work; empty `text` → BAD_INPUT.
- [ ] `tests/test_facebook_post_edit.py` -- NEW; cover the I/O matrix including the NO_CONFIRM guard and the no-op-delete fail-closed path.

**Acceptance Criteria:**
- Given the dedicated browser logged into facebook.com, when a test post is edited then deleted end-to-end, then the edited text is visible and the post is gone afterwards.
- Given a delete invocation without the confirmation flag, when it runs, then no mutation is sent and the error is BAD_INPUT.

## Implementation Notes

## Plan Change Log

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

15 findings: 14 patch / 1 verified-clean / 0 reject / 0 defer. 2 HIGH.

- [high][patch] `_fb_post_vars.json` commits live session secrets: `fb_dtsg`/`lsd`/`jazoest` (×2, edit+delete params) + real `story_id`/message text in variables — siblings are clean; strip + fix capture-script sanitize so re-capture never persists them.
- [high][patch] Send JS `window[KEY]=null; fetch(...)` has no sentinel — a timed-out `cdp.call` retry re-runs the eval and FIRES the mutation twice; result key clobbered. Same root cause in all 4 write modules; add `window[KEY+'_sent']` guard.
- [medium][patch] `permalink.php?story_fbid=` inputs always fail: `_ensure_post_loaded` compares raw path vs FB-302-canonical href → UPSTREAM_BROKEN despite advertised support (and `facebook post` outputs exactly this form — piping its output into any write action breaks). Adopt canonical `location.href` / compare resolved post identity.
- [medium][patch] `_resolve_audience` accepts first `base_state` in HTML without checking page still shows target post → silent wrong audience.
- [medium][patch] Audience preservation restores only `base_state`; `allow`/`deny`/`tag_expansion_state` from skeleton — editing a "Friends except X" post silently broadens visibility. Extract full `privacy` object; fail closed.
- [medium][patch] `_check_graphql` raises on any `errors` before positive signal → confirmed delete with partial errors reported as failure.
- [medium][patch] `_deleted_post`/`_edited_post` substring-search 4000-char truncated `body` while parsed `deletedStoryId`/`storyId` fields exist; edit never verifies target storyId.
- [low][patch] `__FB_AUDIENCE` substituted after `__FB_TEXT` — text containing the sentinel → JS SyntaxError.
- [low][patch] `\d`/`isdigit()` accept non-ASCII digits → BAD_INPUT should fire instead of confusing UPSTREAM_BROKEN.
- [low][patch] `message.ranges` kept from skeleton after text substitution → stale mention offsets.
- [medium][patch] `_eval(...,10)` hardcoded timeout inside `left()`-budgeted loops → can burn ~42s and starve mutation phase.
- [low][patch] `_STORY_LOCATE_JS` dedupes raw base64 → padded+unpadded same id counts twice → spurious ambiguity.
- [low][patch] Test gaps: permalink.php redirect, errors+data partial, `__FB_AUDIENCE` text, echo cutoff, allow/deny, unicode digits, sentinel no-refire.
- [n/a][verified-clean] bool Param plumbing through cli.py + dispatch.py — no defect.



## Design Notes

The delete guard is a CLI flag (`--confirm`), validated in the channel method before importing the write module's heavy path — the matrix's "before browser work" requirement means the test can assert zero evals fired. Story id resolution: the `facebook post` response already returns `storyId`/`postId`; accept either raw form or a permalink, resolving live only for permalinks. Test post (own, only_me — create one with `agent-reach facebook post` first if the standing post was deleted): `https://www.facebook.com/61554949615037/posts/122311668092164987`.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` -- expected: all pass (baseline 706).
- `agent-reach facebook post "edit-delete e2e" --audience only_me` then `agent-reach facebook post-edit <url> "edited text"` then `agent-reach facebook post-delete <url> --confirm` -- expected: each ok:true; post gone on facebook.com.
- `agent-reach facebook post-delete <url>` (no flag) -- expected: ok:false, BAD_INPUT, post untouched.

### 2026-11-21 — Live-e2e finding (unicode echo false-negative)

- `[medium][patch]` `agent_reach/channels/_facebook_post_edit.py:220` — `out.textEchoed=t.indexOf(JSON.stringify(text))>=0` fails on non-ASCII text: FB JSON-escapes response bodies with `\uXXXX`, so every Vietnamese/emoji edit reported UPSTREAM_BROKEN while the mutation actually landed (verified: post text DID change despite error). Fix: check both raw needle and `\u`-escaped form (`_txtEsc`). Regression test: `test_edit_text_echo_check_is_unicode_aware`. Live-verified: post-edit "đã edit tiếng Việt + emoji sau fix ✏️🇻🇳" → ok:true, storyId echo, audience SELF preserved.

## Auto Run Result

- **Delivered**: `agent-reach facebook post-edit <url> "new text"` + `agent-reach facebook post-delete <url> --confirm` — replay real mutations `ComposerStoryEditMutation` (doc_id 30241673288768290) + `useCometFeedStoryDeleteMutation` (doc_id 29557927263819997) via fire-and-poll CDP fetch.
- **Files**: `_facebook_post_edit.py` (~570 lines: `facebook_post_edit`, `facebook_post_delete`), `_fb_post_vars.json` (sanitized `{params, edit{variables}, delete{variables}}` skeleton), `scripts/capture_fb_post_edit.py`, `tests/test_facebook_post_edit.py` (60 tests), `facebook.py` (+2 actions), `runtime/cli.py`+`dispatch.py` (generic bool-Param support — `--confirm` flag).
- **Review pass 1 (quick)**: 15 findings → 14 patched (secrets stripped from skeleton, `_sent` idempotent sentinel in all 4 write modules, permalink→canonical href identity compare, full privacy object preserve, positive-signal-before-errors, bounded evals, sentinel order, unicode digits, ranges clear, b64 dedupe) / 1 verified-clean (bool-Param plumbing) / 0 defer.
- **Live e2e**: post→edit→delete confirmed on own only_me timeline; `post-delete` without `--confirm` → BAD_INPUT before browser; unicode-echo bug found live + fixed + regression test.
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → 766 passed.
- **Residual risks**: doc_id rotation on FB deploys (re-capture via `scripts/capture_fb_post_edit.py`); FB delete is idempotent (re-delete on stale permalink still ok:true).
