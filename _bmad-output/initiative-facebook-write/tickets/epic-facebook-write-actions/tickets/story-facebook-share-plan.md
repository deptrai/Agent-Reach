---
title: facebook share
type: feature
ticket: '5'
created: 2026-11-21
status: built
baseline_revision: d972494f6d5c30fceb1430c05bde0d8420a7986e
route: full
route_source: auto
risk: low
review: quick
review_source: pinned
lenses_ran: []
followup_review_recommended: false
deferred: []
warnings: []
context:
  - agent_reach/channels/_facebook_write.py
  - agent_reach/channels/_facebook_react.py
  - agent_reach/channels/_facebook_comment.py
  - agent_reach/channels/facebook.py
  - tests/test_facebook_react.py
  - scripts/capture_fb_comment.py
  - docs/fb-mutation-capture.md
---

## Intent Contract

`agent-reach facebook` hỗ trợ post/react/unreact/comment/uncomment/post-edit/post-delete nhưng chưa share. Story này bổ sung `facebook share <post-url> [--text ...] [--audience only_me|friends|public]` — share post lên timeline của chính mình, audience mặc định `only_me`. Capture share mutation thật một lần, lưu skeleton `{params, variables}` đã sanitize, replay qua pipeline fire-and-poll hiện có.

**Always:**
- Replay mutation `/api/graphql` thật đã capture + sanitize (actor/uid → "0", uuid → zero-uuid, epoch → 0, KHÔNG lưu fb_dtsg/lsd/jazoest/av/__user/__spin_* — những key này JS override từ live page).
- Reuse `_eval`, `_fb_target`, `_wait_ready`, `left(cap)` deadline, per-call uuid result key từ `_facebook_write.py`; reuse `_check_url`, `_same_post_path`, `_ensure_post_loaded` từ `_facebook_react.py` — import, không copy.
- Typed `ReachError`: `BAD_INPUT`, `NEED_LOGIN`, `RATE_LIMITED` (HTTP 429), `UPSTREAM_BROKEN`. Fire-and-poll, không blind retry.
- Fire qua `Runtime.evaluate` `credentials:"include"` trên facebook.com page target.
- **Idempotent send**: send JS phải có sentinel `window[KEY+"_sent"]` set trước khi fetch, skip fire nếu đã tồn tại (chống `_eval` retry bắn mutation 2 lần).
- Sentinel substitution: `__FB_TEXT` replace CUỐI (user text có thể chứa `__FB_*` khác).
- Audience map giống post: `only_me→SELF`, `friends→FRIENDS`, `public→EVERYONE`; mặc định `only_me`; giá trị lạ → BAD_INPUT trước khi đụng browser.

**Never:**
- Không Puppeteer/DOM-click làm shipped path; không export credential.
- Không sửa read actions hoặc post/react/comment/post-edit/post-delete.
- Không share lên group/page/timeline người khác — chỉ own timeline.
- Không quảng bá action này làm spread tool — `audience` default `only_me` là safety rail.

## I/O & Edge-Case Matrix

| Case | Input | Expected |
|---|---|---|
| happy | `facebook share <url>` (own hoặc public post) | ok:true + item url/permalink hoặc post id |
| text | `facebook share <url> --text "ghi chú"` | ok:true, shared post có text kèm |
| audience | `--audience friends` | base_state FRIENDS |
| BAD_AUD | `--audience banana` | BAD_INPUT trước browser |
| BAD_URL | non-facebook url | BAD_INPUT trước browser |
| NOT_OWN_UNSHAREABLE | post không cho share | GraphQL error → UPSTREAM_BROKEN |
| NOT_LOGGED_IN | no_tokens hasUid:false | NEED_LOGIN |
| RATE | HTTP 429 | RATE_LIMITED |

## Code Map

| Path | Action | Why |
|---|---|---|
| `scripts/capture_fb_share.py` | create | Mirror `capture_fb_comment.py`; capture share mutation (share dialog → share-to-timeline hoặc share-now) → skeleton |
| `agent_reach/channels/_fb_share_vars.json` | create | `{params, variables}` sanitized — record actual `fb_api_req_friendly_name` + `doc_id` trong code comment |
| `agent_reach/channels/_facebook_share.py` | create | `facebook_share(*, url, text, audience, timeout)` — resolve source post feedback_id từ live page, replay share mutation |
| `agent_reach/channels/facebook.py` | edit | +`Action("share", ...)` backends `(("opencli","_share"),)` |
| `tests/test_facebook_share.py` | create | Mirror `test_facebook_react.py` harness |

**Shared post**: source post của share mutation có thể dùng `feedback_id` (như react) hoặc `shareable_post_id`/story id — capture sẽ cho biết. Không được construct id bằng arithmetic; resolve từ live page như `_resolve_feedback_id`.

## Tasks & Acceptance

- [ ] `scripts/capture_fb_share.py` capture được share mutation thật (doc theo `docs/fb-mutation-capture.md`); skeleton ghi `_fb_share_vars.json` đủ relay flags. **Lưu ý**: share flow có dialog — cần click "Share" trên test post rồi chọn audience/confirm trong dialog.
- [ ] `facebook share <url>` e2e live: shared story xuất hiện trên timeline mình (hoặc response xác nhận post id mới), audience đúng.
- [ ] `facebook share <url> --text "..." --audience friends` replay đúng audience + text.
- [ ] `tests/test_facebook_share.py` xanh; `uv run --extra dev python -m pytest tests -x -q` toàn suite xanh.
- [ ] BAD_INPUT paths không đụng browser (`harness["evals"] == []`).

## Implementation Notes

- Test post (own, public-visible để share được — only_me post KHÔNG share được với public): dùng 1 post public trên feed hoặc tạo post public test rồi share về only_me. An toàn nhất: share post PUBLIC của chính mình (`facebook post --audience public` tạo test post, share nó về `only_me`).
- Share với default `only_me` share vẫn tạo post mới trên timeline — verify qua `story.url`/post_id trong response.
- Response shape share khác post-create: có thể `data.share_story_create` hoặc feed_unit — parse tương tự `storyUrl`/`storyId`/`postId`, fail-closed nếu không xác nhận được tạo thành công.
- doc_id rotation: nếu live fail "document not found" → re-capture.

## Plan Change Log

### 2026-11-21 — Khởi tạo
Plan mới cho story 1.5 theo pattern react/comment/post-edit.

## Review Triage Log

## Verification

```bash
uv run --extra dev python -m pytest tests -x -q   # baseline: 706 pass + story tests
agent-reach facebook post "share e2e source 🔄" --audience public   # tạo source post
agent-reach facebook share <source-post-url>                        # share only_me
agent-reach facebook share <url> --text "note" --audience friends   # share có text
agent-reach facebook share <url> --audience banana                  # BAD_INPUT
```
