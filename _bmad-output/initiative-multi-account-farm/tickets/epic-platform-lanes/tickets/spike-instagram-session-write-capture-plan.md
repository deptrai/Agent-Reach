---
type: feature
ticket: '5'
status: built
route: oneshot
risk: high
baseline_revision: 2c3a2ecb74f9d90f178f12350e5ec5c3ecd61634
---

# Spike 15.5 — Instagram session + write capture feasibility

## Verdict

**NEED_LOGIN — write chưa replayable.** Dedicated browser (CDP `127.0.0.1:9222`) mở `https://www.instagram.com/` nhưng dừng ở màn chọn tài khoản (CAA one-tap), không phải feed. Cookie đăng nhập `sessionid` (HttpOnly, đúng `login_cookie` của `InstagramChannel`) **không có** trong cookie jar sống và cũng không có trong `~/.agent-reach/browser/session.json`. Không tự bấm đăng nhập, không arm interceptor, không bắn mutation.

Story 15.6 **không được** implement `instagram post` trên bằng chứng spike này. Post (kể cả caption-only) phụ thuộc pipeline media; comment/like mới là verb form-encoded đơn giản — nhưng cả hai đều **chưa capture sống**, vì không có session.

## Session (live)

- Tab mới `https://www.instagram.com/`, `document.readyState=complete`, title `Instagram`.
- UI: nút `继续` / `使用其他主页` / `创建新账户`. Nhãn profile nhớ trên picker: `epsiloncryptoai`. Không có `input[name=username]`, không có ô password, không có `article` / `nav` / `main` (feed không render).
- `window._sharedData` chỉ có key `platform_install_badge_links` — không có `config.viewerId`. `window.__additionalDataLoaded` là `undefined`.
- HTML login-wall có chuỗi `caa_login` + `Polaris`; **không** chứa `/api/v1/media/configure`, `/rupload_igphoto`, `/api/v1/web/comments/`, `/api/v1/web/likes/`.
- `performance` resource: 47 request, **0** URL chứa `/api/`, `graphql`, hoặc `rupload`. Host: `static.cdninstagram.com`, `www.instagram.com`, `scontent.cdninstagram.com`, `www.facebook.com`.
- Cookie Instagram (tên thôi, không ghi value): `csrftoken`, `datr`, `dpr`, `ds_user_id`, `ig_did`, `ig_nrcb`, `mid`, `ps_l`, `ps_n`, `wd`. Có `ds_user_id` + `csrftoken` = profile đã nhớ, không phải session. `document.cookie` cũng không có `sessionid` (đúng vì HttpOnly — và `Storage.getCookies` xác nhận jar không có tên đó).

Operator: đăng nhập Instagram trong dedicated browser (bấm `继续` hoặc login đủ) đến khi feed render **và** cookie `sessionid` xuất hiện, rồi mới capture. Spike không làm bước đó.

## Write — đã chứng minh vs giả định

**Đã chứng minh:** không có authenticated session; trang không phát request write; interceptor không có gì để bắt.

**Giả định (chưa chứng minh live — không có session để probe):**

| Verb | Shape công khai | Replay so với X/FB | 15.6 |
|---|---|---|---|
| `instagram post` | Bắt buộc 2 bước: `PUT /rupload_igphoto/` (raw bytes) lấy `upload_id`, rồi `POST /api/v1/media/configure/` (caption + `upload_id`). Không có post chữ thuần. | Khác hẳn GraphQL JSON. Cùng lớp X media đã descope: upload binary, id một lần, không phải fire-and-poll một body. | **Descope.** Không làm trong 15.6. |
| `instagram comment` | `POST /api/v1/web/comments/<media_id>/add/` form-encoded (`comment_text` + csrf). | Gần pattern replay một POST same-origin, `credentials: include`. Csrf lấy từ cookie `csrftoken` (không HttpOnly) khi đã login. | **Scope đề xuất**, nhưng chỉ sau 1 capture sống. |
| `instagram like` | `POST /api/v1/web/likes/<media_id>/like/` (và unlike đối xứng). | Cùng họ form POST, body mỏng hơn comment. | **Scope đề xuất**, cùng điều kiện capture. |

Configure-only (bỏ bước upload) không tạo được post: configure cần `upload_id` từ rupload. Đó là lý do descope post, không phải vì "chưa thử". Chưa thử vì NEED_LOGIN — và thử configure-only khi chưa login cũng không ra skeleton.

## Khuyến nghị story 15.6

1. Chặn code cho đến khi operator login xong. Verify lại: feed render + `sessionid` có trong `cdp.cookies("instagram.com")`.
2. Đổi verb của 15.6 từ `instagram post` sang **`instagram comment` + `instagram like`**. Capture 1 POST thật mỗi verb qua interceptor `window.__cap` (fetch + XHR, lọc `/api/v1/` POST) rồi mới chốt header/body. Đừng hardcode shape ở bảng trên.
3. `instagram post` (rupload + configure) là story riêng, sau comment/like, cùng kiểu descope media của X thread. Không nhét vào 15.6.
4. Ledger: khi ship, map comment/like vào `WRITE_ACTIONS` như các write verb khác. Ngoài scope spike này.

## Đã bỏ qua

Không click `继续`, không gõ mật khẩu, không gửi comment/like/post, không ghi value cookie/csrf/`ds_user_id` vào file này. Rủi ro: bảng endpoint là kiến thức bề mặt, có thể lệch build hiện tại của IG — capture sau login là bắt buộc trước khi code.
