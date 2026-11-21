---
title: 'Refactor sweep'
type: feature
ticket: '7'
created: '2026-11-21'
status: built
baseline_revision: 74e76c7a04f5ba52eefc3791ee330e6997e2f010
route: full
route_source: auto
risk: medium
review: quick
---

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

- [medium][patch] `_facebook_react.py` `_LOCATE_JS` — unescape `&quot;` toàn innerHTML mở false-positive window (user-text `&quot;` → `"` giả → fake fid / truncated capture → silent fallback wrong target). Fix: dual-pattern match trên raw HTML (`"feedback_id":"..."` | `&quot;feedback_id&quot;:&quot;...&quot;`), `indexOf(nodeId)` cho comment-binding.
- [verified-clean][reject] Toàn bộ refactor mechanics — `send_mutation`/`classify_result`/`load_skeleton`/`load_skeleton_sections`/`prepare_facebook`/`_result_key` behaviour-identical với 5 bản inline cũ; `eval_fn` preserve per-module monkeypatch; `http_label` reproduce đúng message strings; no dead imports; `confirmed()` ordering giữ nguyên.

## Intent Contract

**Goal:** dedupe scaffolding replay giữa các mutation modules — pure refactor, zero behaviour change. Guard: 793 tests green.

**Always:** helpers chung vào `agent_reach/channels/_facebook_write.py` (đã là shared-import root); public signatures `facebook_post/react/unreact/comment/uncomment/post_edit/post_delete/share` KHÔNG đổi; typed ReachError giữ nguyên; skeleton files không đổi.

**Never:** đổi JS send exprs' semantics, đổi result shape (`make_item`), đổi classification codes, đổi `doc_id`/skeleton files, xoá tests.

## Duplication → consolidation map

1. **Skeleton loading** (identical ở `_facebook_write.py:40-47`, `_facebook_react.py:56-63`, `_facebook_share.py:48-54`): `load_skeleton(filename) -> (params, vars)` helper; comment/post_edit multi-mutation dùng variant trả `{params, sections}`.
2. **Fire-and-poll send loop** (`_facebook_comment.py:279` `_send_mutation`, `_facebook_share.py:166` `_send_share`, `_facebook_react.py` `_send_reaction`, `_facebook_post_edit.py` send, `_facebook_write.py` `facebook_post` inline): shared `send_mutation(expr, *, key=None, deadline, poll_expr=None) -> dict` — per-call uuid key, `window[key]` poll + `_eval` retry + `no_tokens`/`hasUid` handling, transport-error classification chung (429→RATE_LIMITED, 5xx→UPSTREAM_BROKEN, login-shaped→NEED_LOGIN, `errors[]`→UPSTREAM_BROKEN với `_error_message`).
3. **`session_reuse.prepare("facebook")`** (×5 entrypoints): `prepare_facebook()` helper.
4. **Deferred settle (1.2 review):** `_LOCATE_JS`/`_resolve_feedback_id` không match `feedback_id` khi HTML-escaped `&quot;` — harden regex/normalize trong `_facebook_react.py:83`, nếu thật sự có thể xảy ra thì sửa, nếu không để nguyên + ghi rõ.

## Tasks

1. Hoist shared helpers vào `_facebook_write.py` (`load_skeleton`, `prepare_facebook`, `send_mutation`, `classify_result`).
2. Re-point 4 modules — imports thay code trùng; giữ module-specific result checks (`_deleted_comment`, `_edited_post`, `_reaction_result`, `_share_result`).
3. Settle deferred escaped-feedback_id finding.
4. `uv run --extra dev python -m pytest tests -x -q` → 793 pass, diff thuần refactor (không đổi behaviour).

## Auto Run Result

**Route full — hoàn tất.** Dedupe scaffolding giữa 5 write modules: shared helpers hoisted vào `_facebook_write.py` (`load_skeleton`, `load_skeleton_sections`, `prepare_facebook`, `_result_key`, `send_mutation`, `classify_result`); `_facebook_react/_facebook_comment/_facebook_post_edit/_facebook_share` re-point (`eval_fn` param preserve per-module monkeypatch). Diff **+221/−335 (net −114)** trên 5 files; public signatures, skeleton JSON, JS send exprs, `make_item` shapes không đổi; **793 tests pass** (2 lần verify độc lập).

Deferred finding (1.2) **settled — bug thật, đã fix đúng cách**: `_LOCATE_JS` không match `&quot;`-escaped `feedback_id` trong bootloadable payloads. Bản fix đầu (unescape toàn innerHTML) bị review bắt được — mở false-positive window qua user-text `&quot;`. Fix cuối: dual-pattern match trên raw HTML (`"feedback_id":"..."` | `&quot;feedback_id&quot;:&quot;...&quot;` theo document order), comment-binding dùng `h.indexOf(nodeId)` + cùng alternation trên `seg` raw.

**Verification:** pytest 793 ✓ (sau fix); live e2e smoke không chạy được — session Facebook trong dedicated browser + mọi profile user đều hết hạn (cookie `checkpoint`, FB ép re-auth); không phải lỗi refactor (page state xác nhận `uid:"0"`, login wall). E2e smoke nợ lại khi session được khôi phục.

**Residual risks:** `send_mutation`/`classify_result` là shared path — failure mode mới của FB giờ chỉ sửa 1 chỗ (mục đích refactor); e2e chưa xác nhận sau refactor vì session chết.
