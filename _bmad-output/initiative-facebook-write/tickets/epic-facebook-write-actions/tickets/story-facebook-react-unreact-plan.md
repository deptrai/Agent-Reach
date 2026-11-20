---
title: 'facebook react / unreact'
type: feature
ticket: '2'
created: '2026-11-21'
status: built
baseline_revision: 'eb95d9187c2665e1e2afeb5e2adf403b814a79be'
route: full
route_source: auto
risk: medium
review: quick
review_source: pinned
lenses_ran: [quick]
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
deferred:
  - summary: >-
      Docs (README.md, docs/README_en.md, SKILL.md, SKILL_en.md, onboarding.py) still claim likes/reactions are prohibited.
    evidence: |-
      Real — same contradiction class as the post-action D1 fix. Owned by epic story 1.6 (write-surface docs update), not this story.
    location: >-
      README.md:184; docs/README_en.md:111,184; agent_reach/skill/SKILL.md:9,19,104; SKILL_en.md:10,19,104; agent_reach/runtime/onboarding.py:39
    severity: medium
  - summary: >-
      `_LOCATE_JS` comment binding may miss HTML-escaped `&quot;feedback_id&quot;` blobs, silently falling back to the post's feedback_id.
    evidence: |-
      Unverified — settle by reacting on a comment permalink (?comment_id=) and checking the reaction lands on the comment, not the post.
    location: >-
      agent_reach/channels/_facebook_react.py:83
    severity: medium (unverified)
---

<intent-contract>

## Intent

**Problem:** Agent Reach can post to Facebook but cannot react/unreact — the most common FB interaction. R1 requires `facebook react <post-or-comment-url> --type like|love|haha|wow|sad|angry` and `facebook unreact <url>`.

**Approach:** Capture the real UFI reaction + remove-reaction mutations once via the CDP capture recipe (like story 1.1 did for post), store sanitized `{params, variables}` skeletons, then replay with live tokens through the same fire-and-poll pipeline as `facebook_post`.

## Boundaries & Constraints

**Always:** Reuse `_facebook_write.py` machinery (`_fb_target`, `_eval`, `_wait_ready`, shared-deadline `left(cap)`, per-call uuid result key, `_eval`/`session_reuse.prepare`). Skeletons sanitized (actor_id/user ids → "0", uuids → zero-uuid, epoch ms → 0). Typed `ReachError` (NEED_LOGIN/RATE_LIMITED/UPSTREAM_BROKEN/BAD_INPUT). Fire-and-poll — never blind-retry a sent mutation.

**Never:** No Puppeteer, no credential export, no DOM-click automation as the shipped path (CDP eval/fetch only). No writes outside the facebook channel. Do not change read actions (`search`/`profile`) or `post`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | `react <post-url> --type love`, logged in | `ok:true`, item with target url + reaction type | — |
| UNREACT | `unreact <post-url>` after react | `ok:true`, reaction removed | — |
| BAD_TYPE | `--type banana` | — | BAD_INPUT before any browser work |
| NO_FB_ID | URL has no resolvable feedback_id (not a FB post, private/deleted) | — | BAD_INPUT or UPSTREAM_BROKEN "could not resolve feedback_id" |
| NOT_LOGGED_IN | no `c_user`/tokens on page | — | NEED_LOGIN (same wording family as post) |
| RATE_LIMIT | FB answers HTTP 429 | — | RATE_LIMITED |

</intent-contract>

## Code Map

- `agent_reach/channels/_facebook_write.py` — copy this pattern: `_POST_JS` (fire-and-poll fetch), `_fb_target`, `_eval`, `_wait_ready`, `_error_message`, `facebook_post` deadline structure. Reuse by import or by extracting shared helpers into this file — implementer's call, keep post's public behavior identical.
- `agent_reach/channels/_facebook_vars.json` — skeleton format precedent: `{"params": {...}, "variables": {...}}`; new files follow it.
- `agent_reach/channels/facebook.py` — add `Action("react", ...)` (params `url` required, `type` required, default `like`) and `Action("unreact", ...)` (param `url`), both `backends=(("opencli","_react"/"_unreact"),)` for browser-serialization semantics; validate `type`/`url` in Python before browser work.
- `tests/test_facebook_write.py` — monkeypatch-harness precedent (`fake_eval` distinguishing send vs poll exprs); new test file `tests/test_facebook_react.py` mirrors it.
- `scripts/capture_fb_mutation.py` + `docs/fb-mutation-capture.md` — capture recipe. **Unattended equivalent (proven today):** raw `agent_reach.runtime.cdp` — `cdp._json('/json/list')` → facebook.com `page` target → `Runtime.evaluate` arm `window.__fbCap` interceptor on `XMLHttpRequest` + `fetch` (store `{n: friendly_name, b: body}` for `/api/graphql`) → click Like on a test post → read `__fbCap` → `parse_qsl` body → `doc_id` + `variables`.
- Test post for capture/verify: `https://www.facebook.com/61554949615037/posts/122311659992164987` (only_me, safe to like/unlike repeatedly).

## Tasks & Acceptance

**Execution:**
- [ ] capture — arm interceptor on the test post page in the dedicated browser, click Like then Unlike via eval, dump the two mutation bodies, sanitize (same rules as `capture_fb_mutation.py`), write `agent_reach/channels/_fb_react_vars.json` (+ `_fb_unreact_vars.json` if unreact is a distinct friendly_name — capture reveals; if unreact is just `reaction_type:"NONE"` on the same mutation, one skeleton suffices)
- [ ] `agent_reach/channels/_facebook_react.py` — `facebook_react(*, url, reaction, timeout)` + `facebook_unreact(*, url, timeout)`: prepare → target → wait_ready → resolve `feedback_id` (eval regex/`require`-module lookup on the post URL — navigate via `Page.navigate` or new tab if needed; for comment URLs prefer a `feedback_id` bound to the comment, else first post feedback_id) → fire-and-poll send → typed errors + result `make_item`
- [ ] `agent_reach/channels/facebook.py` — `react` + `unreact` actions wired to `_react`/`_unreact`
- [ ] `tests/test_facebook_react.py` — harness + parametrized reaction types, unreact, bad type/url, NEED_LOGIN/429/error mapping, per-call key

**Acceptance Criteria:**
- Given a logged-in dedicated browser, when `agent-reach facebook react <test-post-url> --type love` runs, then `ok:true` and the love reaction is visible on the post
- Given a reacted post, when `agent-reach facebook unreact <url>` runs, then `ok:true` and the reaction is gone
- Given `--type banana`, when `react` runs, then BAD_INPUT without touching the browser
- Given a non-FB or dead URL, when `react` runs, then a typed error (no unhandled crash, no phantom success)

## Implementation Notes

## Plan Change Log

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)
- verdicts: 7 findings — high 0, medium 4, low 1, false 1, maybe-false 1
- findings:
  - `[medium]` `[patch]` `_ensure_post_loaded` navigate-then-check race — `Page.navigate` is fire-and-forget; `_wait_ready` can pass on the old doc so `_resolve_feedback_id` may return the previous post's feedback_id → wrong-target reaction. Fix: poll `location.href` until `_same_post_path` before proceeding.
  - `[medium]` `[patch]` `_reaction_result` never asserts reaction state — `reactionId`/`reactionCount` extracted but unused; silent no-op → `ok:true`. Fix: require non-None reactionId on react / removed state on unreact else UPSTREAM_BROKEN.
  - `[false]` `[reject]` unreact `feedback_reaction_id "0"` unproven — disproven by live e2e this session: `unreact` returned reactionCount 0 and the reaction is gone on FB.
  - `[medium]` `[patch]` `capture_fb_mutation.py` writes bare `variables` only — re-capture would silently drop the params template. Fix: emit `{"params","variables"}`.
  - `[medium]` `[defer]` docs (README×2, SKILL×2, onboarding) still say likes prohibited — owned by epic story 1.6 (write-surface docs update); out of this story's scope.
  - `[low]` `[patch]` `_check_url` catches only `ValueError` — `TypeError` for non-str leaks. Fix: `except (ValueError, TypeError)`.
  - `[maybe-false]` `[defer]` `_LOCATE_JS` misses HTML-escaped `&quot;feedback_id&quot;` → wrong-target fallback. Settle by: react on a comment permalink page and check binding.

## Design Notes

Reaction types on FB web: `LIKE`, `LOVE`, `HAHA`, `WOW`, `SAD`, `ANGER` — keep the user-facing CLI types lowercase (`like|love|haha|wow|sad|angry`) and map `angry→ANGER`. `feedback_id` is a base64 GraphQL node id embedded in the post page (`"feedback_id":"..."` in page source/`require` bootstraps) — resolving it inside the live page (eval) is the reliable path; constructing it from post_id is fragile and forbidden.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` -- all pass (baseline: 641)
- `agent-reach facebook react https://www.facebook.com/61554949615037/posts/122311659992164987 --type love` -- `ok:true` + post shows 1 love
- `agent-reach facebook unreact https://www.facebook.com/61554949615037/posts/122311659992164987` -- `ok:true` + reaction removed

## Auto Run Result

- **Summary**: `facebook react <url> --type like|love|haha|wow|sad|angry` + `facebook unreact <url>` — replay `CometUFIFeedbackReactMutation` (doc_id 27646120298312844, cùng mutation cho cả hai: unreact = `feedback_reaction_id "0"`) qua fire-and-poll trong dedicated browser; `feedback_id` resolve live từ post page (comment permalink → best-effort binding).
- **Files**: `_facebook_react.py` (336 lines — module chính, tái dùng `_fb_target`/`_eval`/`_wait_ready`); `_fb_react_vars.json` (skeleton params+variables, bỏ tracking sau khi chứng minh không cần); `facebook.py` (+34 — 2 actions); `tests/test_facebook_react.py` (32 tests); `capture_fb_mutation.py` (sửa: dump `{params, variables}`).
- **Review**: 7 findings → 4 patch (navigate-race→href poll; state-verify reactionId/rid; script params; non-str url), 1 reject (unreact "0" — live-verified), 2 defer (docs→story 1.6; escaped feedback_id).
- **followup_review_recommended: true** — 3 medium entries patched; risk chưa verify: comment-permalink binding chưa được test thật (không có comment trên test post).
- **Verification**: 673 tests pass (baseline 641 + 32); live `react --type wow` → reactionId 478547315650144 count 1; `unreact` → count 0; `--type banana` → BAD_INPUT no browser.
- **Residual risks**: doc_id rotation (re-capture recipe documented); comment binding fallback could react on post instead of comment (deferred, unverified).
