---
type: feature
ticket: '7'
status: built
route: oneshot
risk: high
baseline_revision: 2c3a2ecb74f9d90f178f12350e5ec5c3ecd61634
---

# Spike 15.7 — TikTok session + write capture feasibility

## Verdict

- **post (video/caption) → DESCOPE.** TikTok video post là chunked binary
  upload nhiều request (`web-upload` / `/aweme/v1/upload/` + metadata create
  `/api/v1/item/create/` cần upload-session binding từ các chunk ACK), không
  phải 1 mutation replayable — cùng lý do descope media của X (story 12.6).
- **like (`/api/commit/item/digg/`) + comment (`/api/comment/publish/`) →
  REPLAYABLE, điều kiện session.** X-Bogus mint được thuần page context
  (`window.byted_acrawler.frontierSign(url) → {"X-Bogus": "..."}` — đã mint
  thử thành công trên tab guest), msToken/tt_csrf_token nằm trong cookie jar,
  endpoint nằm trong `_mssdk._enablePathList`. Duy nhất còn thiếu **session
  login thật** để capture skeleton và verify live.
- **Session: NEED_LOGIN cho operator.** Trình duyệt dedicated hiện là guest —
  tab `/explore` render nút "登录"/"上传" nav khách, `webapp.app-context`
  không có key `user`, cookie jar chỉ có `ttwid`/`tt_csrf_token`/`tt_chain_token`/
  `msToken` (guest-class), KHÔNG có `sessionid`/`sessionid_ss`/`uid_tt`/
  `multi_sids`. Login do operator cung cấp — không tự login.

## Bằng chứng thu thập (live, CDP `127.0.0.1:9222` trên dedicated browser)

- Tạo tab riêng (`Target.createTarget` → `https://www.tiktok.com/`, tự redirect
  `/explore`, region `VN`, appId `1180`). Lưu ý tooling: helper
  `cdp.call(..., page=True, target_id=...)` trả result cho nhỏ (`1+1` OK) nhưng
  object lớn trên tiktok.com nhiều lần timeout trả None — spike dùng bản
  copy-timeout-lớn của chính nó; đừng sửa `cdp.py` cho chuyện này.
- Guest state xác nhận 2 kênh: (1) UI labels chứa `登录` (Log in) + nav
  `上传` (Upload — guest vẫn thấy nút, click sẽ bung login dialog);
  (2) `__$UNIVERSAL_DATA$__['webapp.app-context']` **không có key `user`**
  (chỉ odinId/wid device-level), `login_config.statusCode = 0`.
- `window.byted_acrawler` đã init ngay trên trang anonymous: keys =
  `frontierSign, getReferer, init, isWebmssdk, registerWsSigner, report,
  setTTWebid(V2), setTTWid, setUserMode`. `frontierSign` là vm-shuffled
  thunk (`function _(n){return T(71999,o,this,arguments,0,25)}`) do
  `webmssdk.js 1.0.0.422` + `webmssdk_ex 1.0.0.3005` + `secsdk` đăng ký.
- **Mint test (anonymous page, live):**
  `await byted_acrawler.frontierSign('https://www.tiktok.com/api/commit/item/digg/?aid=1988&item_id=1&type=1')`
  → `{"X-Bogus": "<16-char alnum>"}` — trả Promise, `awaitPromise` của
  Runtime.evaluate cover sẵn. Input chấp nhận cả full-url string, `{url}`
  object, và bare query-string — cùng output. Đây là tương đương
  `__wr('265390').kc()` của X, thậm chí đơn giản hơn (không cần module id,
  không lazy chunk).
- `_mssdk._enablePathList` (209 path — danh sách endpoint được webmssdk hook
  sign) chứa đúng write verbs cần: `/api/commit/item/digg/`, `/api/comment/digg/`,
  `/api/comment/publish/`, `/api/comment/delete/`, `/api/commit/follow/user/`,
  `/api/v1/item/create/`, `/aweme/v1/upload/image2/`.
- Không thấy `X-Gnarly` trong HTML build hiện tại → scheme X-Bogus + msToken
  vẫn là đường ký chính của build này (X-Gnarly mới hơn, chưa bắt buộc ở đây).
- `msToken` (168 chars, readable) + `tt_csrf_token` (36) + `tt_chain_token`
  có sẵn trong `cdp.cookies('tiktok.com')` ngay ở guest — sẽ đổi sau login nhưng
  cơ chế đọc giống hệt (`credentials:'include'` + `document.cookie` cho
  non-httpOnly `msToken`).

## Fetch signature dự kiến cho `tiktok like` / `tiktok comment`

```
POST https://www.tiktok.com/api/commit/item/digg/?aid=1988&...&X-Bogus=<minted>
credentials: 'include'
headers: content-type 'application/x-www-form-urlencoded;charset=UTF-8'
         (+ có thể 'x-tt-csrf-token' = cookie tt_csrf_token — xác nhận khi capture)
body:    item_id=<id>&type=1&digg_source=<source>   // skeleton capture 1 lần

POST https://www.tiktok.com/api/comment/publish/?aid=1988&...&X-Bogus=<minted>
body:    text=<text>&aweme_id=<video_id>&post_from=...   // skeleton capture 1 lần
```

Pattern giống FB/X: warm session → mở tab tiktok.com → arm interceptor
(`scripts/capture_mutation.py --platform tiktok`, marker = path
`/api/commit/item/digg/`) → evaluate send-expression fire-and-poll. Điểm khác
duy nhất: URL query cần `X-Bogus` fresh mỗi call
(`await byted_acrawler.frontierSign(url)` trong cùng expression) — không cần
webpack require như X, không cần `window.require` như FB.

Còn phải xác nhận khi có session: (a) endpoint có đòi thêm `_signature`/
`verifyFp` ngoài `X-Bogus`+`msToken` không (một số build cũ đòi — capture 1
request thật sẽ trả lời); (b) header `x-tt-csrf-token`/`tt_chain_token`
header-mirror; (c) `aid/app_name` param set đúng cho web (aid=1988,
app_name=tiktok_web trên desktop).

## Notes cho operator

- Để build lane like/comment: login tài khoản test trên dedicated browser
  (mở https://www.tiktok.com/login), rồi chạy lại probe — sentinel cookie
  `sessionid`/`sessionid_ss` xuất hiện là đủ bằng chứng NEED_LOGIN→OK
  (theo pattern `login_cookie` của `_opencli_site.py`).
- Capture skeleton sau login: bắn 1 like thật trên video công khai bất kỳ
  (không nhất thiết trong farm), sanitize giống `_x_*_vars.json`.
- Post/video: KHÔNG nằm trong lane — descope cố định (chunked binary upload).
