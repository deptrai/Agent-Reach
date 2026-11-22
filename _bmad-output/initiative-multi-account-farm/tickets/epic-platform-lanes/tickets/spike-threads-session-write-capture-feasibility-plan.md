---
title: 'Spike 15.9 — Threads session + write capture feasibility'
type: feature
ticket: '9'
created: '2026-11-22'
status: built
baseline_revision: 90abafb86b40245121468f4b97302ef39665bbbc
route: oneshot
route_source: auto
risk: high
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

**Problem:** Threads (threads.com, Meta) chưa có channel trong repo; trước khi build lane `threads post` (story 15.10) phải chứng minh được create-post mutation replayable và session model rõ — sai ở đây thì 15.10 build trên giả định.

**Approach:** Probe live trên dedicated browser (CDP 127.0.0.1:9222) theo đúng pattern TikTok spike 15.7: kiểm tra session state → nếu đã login thì arm interceptor trên threads.com target, tạo 1 post text thật qua composer, đọc captured request (endpoint + `friendlyName` + vars shape + doc-id persisted?) → ghi verdict replayable/descope vào plan này.

</intent-contract>

## Spike Questions (bắt buộc trả lời trong verdict)

1. **Session model** — Threads login = Instagram-linked identity qua Meta Accounts Center: threads.com cần login riêng hay reuse cookies của session instagram.com đã có trên dedicated browser (IG lanes verified trước đây)? Quyết định `add` onboarding + `NEED_LOGIN` path của channel tương lai.
2. **Create-post mutation** — endpoint URL + `fb_api_req_friendly_name`/operation name + variables shape + doc-id có persisted không (private API giống Instagram). Nếu threads.com dùng REST endpoint thay GraphQL persisted → ghi rõ.
3. **Media attach** — image upload có nằm trong 1 request hay chunked flow riêng? Chunked (giống IG media pipeline/TikTok) → descope caption-only, ghi vào verdict.
4. **Dead-doc signature** — gửi lại request đã capture với `variables` rỗng: response có phân biệt được "query alive / doc-id dead" không (khuôn `probe_facebook`, `agent_reach/runtime/query_ids.py`)? Nếu không → Threads doc-id chỉ bắt ở send time qua `PERSISTED_QUERY_DEAD`; ghi rõ vì `farm_drift` hiện chỉ live-probe Facebook + cross-check `x`.

## Probe Procedure

1. Launch dedicated browser nếu chưa chạy (`browser.setup()` — CDP 9222); verify `curl http://127.0.0.1:9222/json/version`.
2. Session check: `cdp.cookies('threads.com')` + `cdp.cookies('instagram.com')`; tạo target `https://www.threads.com/` (`Target.createTarget`), probe login state (composer renderable, `sessionid`/`ds_user_id`-class cookies, không guest wall).
3. **Nếu KHÔNG có session**: không tự login — operator cung cấp. Vẫn thu evidence guest (composer có render không, login redirect, cookie jar nào hiện có), ghi verdict NEED_LOGIN + sentinel cookie để operator check (giống kết luận TikTok spike: replayable-condition + missing session).
4. **Nếu có session**: arm `window.__cap` interceptor bằng `Runtime.evaluate` trên threads.com target (mirror X arm trong `scripts/capture_mutation.py --platform x` — `Page.addScriptToEvaluateOnNewDocument` KHÔNG main-world trên backend này, evaluate sau nav trên đúng target). Mở composer, gõ text, bấm Post → đọc captured POST(s).
5. Từ capture: lấy endpoint + friendlyName + doc_id + variables keys; sanitize mental-check (không ghi credential thật vào disk).
6. Empty-variables probe: re-POST doc_id với `variables: {}` trong page context → ghi response signature.
7. Ghi verdict (replayable + mutation name, hoặc descope + lý do) vào `## Verdict` của plan này — verdict là deliverable của spike.

## Code Map (reuse points)

- `agent_reach/runtime/cdp.py` — `cdp.call`, `_json('/json/list')`, `cookies(host)`, `extension_value`; CDP 9222.
- `agent_reach/runtime/browser.py` — `setup()`/`is_running()`/`dedicated_context()`; profile `~/.agent-reach/browser/profile`.
- `scripts/capture_mutation.py` — X arm pattern + sanitize rules (`_ID_KEYS`, `_X_SECRET_KEYS`) để tái dùng tư duy sanitize.
- `agent_reach/channels/_instagram_write.py` — `require_session` (sessionid+ds_user_id+csrftoken), `_ig_target()` → tương tự `_threads_target()` cho 15.10; `classify_ig` → map `PERSISTED_QUERY_DEAD`.
- `agent_reach/runtime/query_ids.py` — `probe_facebook`/`_DEAD_DOC_NEEDLE` cho câu hỏi 4.
- Sibling spike: `tickets/spike-tiktok-session-write-capture-plan.md` — mẫu verdict/evidence/operator-notes của cùng epic.

## Implementation Notes

Oneshot: spike là investigation — deliverable duy nhất là verdict + evidence trong plan này, không sửa production code. Không ghi credential/session thật vào repo.

Decisions/findings ghi trong lúc chạy:

- Dedicated browser (ShardBrowser) launch tại chỗ qua `browser.setup(headless=True)` — backend `shardbrowser`, opencli_context `cpx9zbgs`.
- `window.__cap` fetch/XHR wrap **bắt trượt create mutation** — request không đi qua `window.fetch`/XHR hooks (Relay giữ transport riêng); phải dùng **CDP `Network.requestWillBeSent`** mới bắt được. Story 15.10 capture tool nên lấy route Network domain.
- Composer Lexical: `execCommand('insertText')` không register vào React state — phải `Input.dispatchMouseEvent` focus + `Input.insertText` (CDP).
- UI tiếng Ba Lan trên account test: "Nowy wątek" / "Opublikuj" / "Usuń" — match theo `div[role=button]` innerText.
- Cleanup: 3 probe posts đã delete qua `useBarcelonaDeletePostWWWMutation`, profile verify `probe:false`.

## Verdict

**SUCCESS — feasible.** Lane `threads post` build được theo pattern `_instagram_write` (in-page fetch fire-and-poll trên threads.com page). Trả lời 4 câu hỏi bắt buộc:

**Q1 Session model**: threads.com cần session RIÊNG — KHÔNG share IG cookies (trước login: IG có sessionid/ds_user_id/csrftoken, threads.com 0 cookie). NHƯNG login 1-click "continue with Meta account" từ session IG trong cùng browser — operator chỉ cần login IG rồi click continue, không nhập credential. Cookies sau login: `sessionid`, `ds_user_id`, `csrftoken`, `rur` (+device `ig_did`, `mid`). Actor id lấy từ **`RelayAPIConfig.actorID`** (= param `av`), KHÔNG dùng `CurrentUser`/`CurrentUserInitialData` (rỗng trên Threads).

**Q2 Create-post endpoint**: **REST, không phải GraphQL** — `POST https://www.threads.com/api/v1/media/configure_text_only_post/` (urlencoded): `caption`, `publish_mode=text_post`, `text_post_app_info` (JSON: `text_with_entities.text`, `composer_session_id` uuid fresh-per-post, `self_thread_context_id` uuid, `reply_control`, `quoted_post_id`), `upload_id`=epoch-ms, `web_session_id` (= `__s`/`x-web-session-id`), `jazoest` (từ getAsyncParams). Headers: `x-ig-app-id: 238260118697367`, `x-csrftoken` (từ threads.com csrftoken cookie), `x-web-session-id`, `x-asbd-id`, `x-bloks-version-id`, `x-instagram-ajax: 0`. **Replayable** — live-verified: 3 posts lên thật (đã xóa hết sau probe). KHÔNG có doc_id → không drift persisted-query cho create. Bonus: delete = `useBarcelonaDeletePostWWWMutation` GraphQL `/api/graphql` persisted-doc (doc_id capture được lúc cleanup) → delete IS doc-id-probeable.

**Response contract** (cho `_send_js` fire-and-poll của 15.10): HTTP 200 + JSON `{"media": {"id": "<pk>_<userpk>", "caption": {"pk", "text", "status": "Active", "created_at", ...}, "text_post_app_info": {"text_fragments": ...}}}` — success detect = status 200 + `media.id` present. Failure = non-2xx hoặc JSON thiếu `media.id` (error shape chưa quan sát — IG private-API family nên kỳ vọng `{"message", "status": "fail"}`; 15.10 classify map non-2xx + schema-mismatch → error class mới, xem Q4). `x-web-session-id` **rotate mỗi request** (`ec0q3k:5d1mr2:ldwl2o` → `bnmyoi:5d1mr2:kdnixm`) — mint/read live tại send time, không hardcode.

**Delta cho ticket 15.10** (sửa description khi build): verb `post` là REST → KHÔNG seed doc-id vào `query-ids.json` cho post (chỉ seed cho delete/query ops nếu có); `classify_threads` KHÔNG map `PERSISTED_QUERY_DEAD` cho create — thay bằng REST schema error class; capture tool phải dùng CDP `Network.requestWillBeSent` (`window.__cap` trượt REST mutation).

**Q3 Media**: descope caption-only giữ nguyên — create đi `publish_mode=text_post`; media đi IG-style chunked upload riêng (`/api/v1/media/` family), chưa capture.

**Q4 Dead-doc probe**: create REST endpoint → KHÔNG probeable theo pattern `probe_facebook` empty-variables; signature drift chỉ phát hiện qua error response lúc send (error class mới analog `PERSISTED_QUERY_DEAD` cho REST schema). `farm_drift` thu Threads doc-ids cho các GraphQL ops (delete/query) được, nhưng create chỉ detect khi gửi.

## Verification

**Manual checks:**
- `curl -s http://127.0.0.1:9222/json | grep threads` — threads.com target tồn tại sau tạo.
- `## Verdict` trong file này trả lời đủ 4 spike questions, nêu rõ replayable + mutation name hoặc lý do descope — khớp `verify` của ticket.

## Review Triage Log

| # | Finding | Severity | Verdict | Fix |
|---|---------|----------|---------|-----|
| 1 | Verdict thiếu response-side evidence cho 15.10 (`_send_js` success detect + `classify_threads` cần response contract) | medium | valid | Probe thêm (Network `getResponseBody`): success envelope `{"media":{"id","caption":{"pk","status":"Active"}}}` + `x-web-session-id` rotate/request đã ghi vào Q2 "Response contract" |
| 2 | Ticket 15.10 description stale về doc-id (post REST không có doc-id → không seed `query-ids.json`, không `PERSISTED_QUERY_DEAD` cho create) | high | valid | Block "Delta cho ticket 15.10" trong verdict Q2 + rewrite description entry 10 trong `tickets.toml` (REST endpoint, mint-live params, REST error class mới) |
| 3 | `spike-159.diff` snapshot cũ (frontmatter pre-review) | — | not-a-defect | Diff artifact only; blob cuối cùng sẽ đúng — no action |
