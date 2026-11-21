---
type: feature
ticket: '1'
status: built
route: oneshot
risk: high
baseline_revision: 9e148d42b6d3c0d792c6e3d547e4c6a0161f5547
---

# Spike 12.1 — X mutation capture feasibility

## Verdict

**REPLAYABLE — GraphQL in-page fetch, đúng pattern Facebook.** Cả 3 token nghi
ngờ nhất (bearer, ct0, `x-client-transaction-id`) đều lấy được từ page context
trên tab x.com của dedicated browser; `x-client-transaction-id` — thứ tưởng là
blocker — generate được thuần JS trong page qua webpack module nội bộ của X,
không cần replay intercepted request. Đã verify end-to-end bằng 1 request READ
thật (HomeTimeline) trả về HTTP 200 với header tự lắp hoàn toàn trong page.
Không cần REPLAN sang XActions-HTTP primary lane.

## Bằng chứng thu thập (live, CDP trên dedicated browser)

- Browser: CloakBrowser headless, CDP `127.0.0.1:9222`, start qua
  `browser.ensure_running()` (watcher tự `restore_session()`).
- Session X **đã login sẵn**: `auth_token` (httpOnly) + `ct0` còn hạn trong
  `~/.agent-reach/browser/session.json` và cookie store sống; tab
  `https://x.com/home` render `SideNav_NewTweet_Button` (compose), không có
  login button.
- Interceptor `window.fetch` + `XMLHttpRequest.prototype` (pattern giống
  `__fbCap`): wrap xong scroll timeline → bắt được ≥10 request
  `x.com/i/api/graphql/<queryId>/<Op>` kèm đủ header auth. Lưu ý:
  `Page.addScriptToEvaluateOnNewDocument` đăng ký OK nhưng script KHÔNG chạy
  trong main world của CloakBrowser → arm trực tiếp bằng `Runtime.evaluate`
  trên page đã load (đủ dùng vì SPA nav không reload document).

## Nguồn từng token (tất cả qua `Runtime.evaluate`)

| Token | Nguồn | Evidence |
|---|---|---|
| `authorization` Bearer | `__wr('973526').Rb()` — exported fn; cũng là hằng public embed trong ≥3 module JS (2407, 564844, 973526) | `Bearer AAAAA…ANRILg…` trùng khớp header request thật |
| `x-csrf-token` | cookie `ct0` đọc từ `document.cookie` | `144f9e9b…` trùng khớp header `x-csrf-token` request thật |
| `x-client-transaction-id` | `await __wr('265390').kc('x.com', path, method)` — export `kc` của module transaction middleware; inner signer lazy-load chunk 59924 → module 564498 | Generate OK cho path tuỳ ý kể cả `/i/api/graphql/<qid>/CreateTweet` POST; id mới mỗi call (per method+path) |
| `auth_token` (cookie) | httpOnly — KHÔNG đọc được bằng `document.cookie`, nhưng `credentials:'include'` tự gắn same-origin; đọc được qua `cdp.cookies('x.com')` nếu cần snapshot | session.json + Storage.getCookies |
| `x-twitter-auth-type` | hằng `OAuth2Session` khi đã login | capture |
| `x-twitter-active-user` / `-client-language` | hằng `yes` / `en` (language lấy từ settings state nếu cần đa tài khoản) | capture |
| module system | `window.webpackChunk_twitter_responsive_web` (163 chunk); push-trick `C.push([[id],{},r=>__wr=r])` → `__wr` (`__webpack_require__`), `__wr.m` = 4695 module | scan source `String(__wr.m[id])` tìm module theo string |

## Fetch signature cho `x post` (mutation tương lai)

```
POST https://x.com/i/api/graphql/{queryId}/CreateTweet
credentials: 'include'
headers:
  authorization:            await __wr('973526').Rb()
  x-csrf-token:             /ct0=([^;]+)/.exec(document.cookie)[1]
  x-twitter-auth-type:      'OAuth2Session'
  x-twitter-active-user:    'yes'
  x-twitter-client-language:'en'
  x-client-transaction-id:  await __wr('265390').kc('x.com', path, 'POST')   // fresh, per (method,path)
  content-type:             'application/json'
body: {"variables": {...}, "features": {...}, "queryId": "..."}   // skeleton capture 1 lần qua interceptor
```

Pattern y hệt FB: warm session → mở tab x.com → evaluate send-expression
fire-and-poll (`window[key]` result, sent-sentinel chống duplicate). Khác FB
duy nhất: token không phải `window.require` mà là webpack `__wr` + `kc()`
async (trả Promise — `awaitPromise: true` của Runtime.evaluate cover sẵn).

## Đã chứng minh vs giả định

**Đã chứng minh (live):** session login OK; interceptor bắt đủ auth header;
bearer/ct0/txn-id lấy được trong page; `kc()` generate txn-id cho path mutation;
READ replay `GET /i/api/graphql/KmJCeZ__ef1fNos7tKN_fg/HomeTimeline` với header
tự lắp → **200 + `{"data":{...}}`**.

**Giả định (chưa bắn thật, đúng scope read-mostly):** body skeleton của
`CreateTweet` (variables/features) — cần 1 lần capture mutation thật qua
interceptor, giống `scripts/capture_fb_mutation.py`; queryId/doc-id của
CreateTweet (rotate theo build → đúng yêu cầu externalize config của epic).

## Rủi ro còn lại

1. **Module id/export churn**: `265390`/`973526`/`kc`/`Rb` đổi theo build →
   lookup bằng string-scan (`'x-client-transaction-id'`, `'Bearer AAA'`) trong
   `__wr.m` mỗi session, không hardcode id. Bearer thì đơn giản hơn: hằng public
   ổn định, embed được.
2. **Feature flag** `rweb_client_transaction_id_enabled` gate middleware — ta
   bypass bằng cách gọi `kc()` trực tiếp; nếu X đổi đường generate → cần re-scan
   module.
3. **Cold-tab lazy chunk**: `kc()` trigger `r.e(59924)` load chunk — đã chạy OK
   trên tab đã hoạt động; cần regression-check trên tab mới tinh.
4. **Per-account queryId drift**: probe ≥2 account trước khi chốt "global theo
   platform" (đã nằm trong Done-when của epic).
5. **ct0/auth_token rotate**: đọc live mỗi send (đã làm); auth_token hết hạn →
   NEED_LOGIN giống FB.
6. **Không có `window.require`**: X dùng webpack — mọi doc nói "giống
   `window.require` của FB" phải hiểu là "webpack require qua push-trick".

## Prerequisite

Dedicated browser phải có session x.com login (`auth_token`+`ct0` hợp lệ) —
**đã có trên host này**. Farm account khác cần session tương đương qua cơ chế
import của epic 11 (account substrate). Guest token (`gt`) chỉ cần cho lane
logged-out — out of scope cho write lane.

## Kết luận cho epic

Gate tracer PASS → tiếp tục theo đúng shape `_facebook_write.py`:
`_twitter_write.py` + `_x_<verb>_vars.json` skeleton + capture harness
generalize từ `scripts/capture_fb_mutation.py` + queryIds trong config
hot-reloadable. Story capture-đầu-tiên phải bắt `CreateTweet` (hoặc mutation
nhẹ hơn như `FavoriteTweet` trên tweet test) trên designated test account.
