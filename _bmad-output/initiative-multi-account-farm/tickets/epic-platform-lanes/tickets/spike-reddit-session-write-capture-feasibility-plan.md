---
title: 'Spike 15.3 — Reddit session + write capture feasibility'
type: feature
ticket: '3'
created: '2026-11-22'
status: built
baseline_revision: 2c3a2ecb74f9d90f178f12350e5ec5c3ecd61634
route: oneshot
route_source: pinned
risk: high
review: none
review_source: pinned
lenses_ran: []
review_loop_iteration: 0
context: []
---

# Spike 15.3 — Reddit session + write capture feasibility

## Verdict

**REPLAYABLE, phân hai nhánh theo mức tin cậy token.**

- **Nhánh cũ (ưu tiên xây): `POST /api/submit` + `uh` modhash.** Modhash là
  CSRF token của legacy API — lấy được trong page qua `<input name="uh">` trên
  `old.reddit.com`, qua `window.postMessageTarget`/bootstrap, hoặc gọi
  `GET /api/me.json` (trả `data.modhash`). Submit qua `application/x-www-
  form-urlencoded`, cookies same-origin đủ — không cần OAuth bearer. Đây là
  path replay đơn giản nhất, giống phần "legacy envelope" của epic 12.
- **Nhánh mới (new UI): `POST https://gql.reddit.com/` operationName
  `CreatePost` (hoặc `SubmitPost`).** Body GraphQL `{operationName, variables,
  extensions.persistedQuery}` — schema đã biết, nhưng **authorization đòi
  OAuth bearer minted per-session** (`Bearer <token>`), kèm các header
  `x-reddit-oidc`, `x-reddit-loid`, `x-reddit-session`, `x-reddit-device-id`.
  Bearer này Reddit web lấy qua `GET https://www.reddit.com/svc/shreddit/token`
  (hoặc `/sv/token`) dùng session cookie `reddit_session` (HttpOnly). Vì
  `reddit_session` là HttpOnly, page JS **không đọc được trực tiếp** — nhưng
  `credentials:'include'` gắn nó tự động, nên token-mint call chạy được trong
  page context y hệt X (`fetch('/svc/shreddit/token', {credentials:'include'})`
  → `access_token`). Header `x-reddit-loid`/`-session` đọc được trong
  `window.__r` bootstrap hoặc `localStorage`.

## Bằng chứng (live, dedicated browser CDP `127.0.0.1:9222`)

- Browser: CloakBrowser/Chrome dedicated của Agent Reach trên port 9222,
  verify qua `browser.is_running()` + `_require_owned()`. Profile riêng
  `~/.agent-reach/browser/profile` — không phải Chrome của user.
- **Session Reddit: CHƯA ĐĂNG NHẬP.** Mở tab `https://www.reddit.com/` (load
  complete, HTTP 200, không bị block). Eval probe trả về:
  `{login:false, account:false, usernameGlobal:false, legacyLoggedIn:false,
  blocked:false}` — không thấy `#login-button`/`USER_DROPDOWN_ID`/avatar
  menu trong header query thường **và** qua Shadow-DOM walk (0 controls khớp
  nghiệm vụ login **hoặc** account). `window.__r`/bootstrap không expose
  `logged===true` hay `USER_NAME`. Đây là probe bất đẳng định (cả login và
  account đều false vì header dùng Shadow DOM + lazy render) — cần probe bổ
  sung `document.cookie` chứ `reddit_session` hoặc gọi `/api/me.json` để chốt.
  → **NEED_LOGIN cho operator**: cần operator đăng nhập Reddit trên tab
  dedicated browser trước khi capture mutation thật.
- Không bắn request ghi nào — đúng scope spike: chỉ verify session +
  tài liệu envelope.

## Nguồn từng token (sau khi operator đã login)

| Token | Nguồn | Cách lấy |
|---|---|---|
| `uh` / `X-Modhash` | modhash CSRF legacy | `GET /api/me.json` → `data.modhash`; hoặc `<input name="uh">` trên old.reddit.com |
| OAuth `Bearer` | mint per-session | `fetch('/svc/shreddit/token',{credentials:'include'})` → `access_token` (HttpOnly session cookie gắn tự động) |
| `reddit_session` | HttpOnly cookie | không đọc trực tiếp; `credentials:'include'` cover |
| `x-reddit-loid` | LOID cookie + bootstrap | `document.cookie` (LOID không HttpOnly) hoặc `window.__r` |
| `x-reddit-session` | tracker session | `window.__r` bootstrap state / `localStorage` |
| `x-reddit-device-id` | device id | bootstrap `window.__r` / cookie `loid`/`pc` |
| `x-reddit-oidc` | OIDC claim | response của token-mint endpoint |

## Envelope đề xuất (nhánh mới, new UI)

```
POST https://gql.reddit.com/
credentials: 'include'
headers:
  authorization:       Bearer <access_token>            // /svc/shreddit/token
  x-reddit-oidc:       <oidc từ token response>
  x-reddit-loid:       <LOID cookie/bootstrap>
  x-reddit-session:    <bootstrap>
  x-reddit-device-id:  <bootstrap/cookie>
  content-type:        application/json
body: {
  "operationName": "CreatePost",
  "variables": { "input": { "subreddit": "...", "title": "...",
                 "body": "...", "kind": "self" } },
  "extensions": { "persistedQuery": { "version": 1,
                 "sha256Hash": "<doc-id — externalize vào query-ids config>" } }
}
```

## Envelope đề xuất (nhánh cũ, khuyến nghị build đầu)

```
POST https://www.reddit.com/api/submit          (hoặc old.reddit.com/api/submit)
credentials: 'include'
headers:
  X-Modhash:     <modhash>
  content-type:  application/x-www-form-urlencoded
body: uh=<modhash>&sr=<subreddit>&title=<t>&text=<body>&kind=self&api_type=json
```

## Nhận định story 15.4 (implementation shape)

- **Khuyến nghị build `/api/submit` trước** — không cần OAuth bearer, modhash
  lấy 1 GET trong page, replay chỉ cần session cookie + `X-Modhash`. Giống
  "legacy envelope" của X: token ít, surface nhỏ.
- Nhánh `gql.reddit.com CreatePost` là dự phòng khi `/api/submit` chết trên
  subreddit/loại post cụ thể — capture 1 lần qua interceptor `window.fetch`
  (pattern `__fbCap`/`__xCap`) trên tab đã login, rồi externalize persisted-
  query hash vào doc-id config (hot-reloadable, theo đúng yêu cầu epic).
- Capture pipeline: arm `window.fetch`+`XMLHttpRequest` interceptor trên tab
  reddit.com (đã load, SPA nav không reload → Runtime.evaluate đủ dùng, cùng
  cảnh báo `Page.addScriptToEvaluateOnNewDocument` không chạy trong main
  world của CloakBrowser như spike X ghi), drive composer trong test context
  → dump `window.__redditCap`, sanitize `authorization`/`x-reddit-*` → skeleton.
- `reddit comment` (ticket 4 kèm) cùng pattern: `POST /api/comment` + `uh`
  (legacy) hoặc GraphQL `CreateComment` (new).

## Blocked / Next step cho operator

- **NEED_LOGIN**: mở `https://www.reddit.com/login` trên dedicated browser,
  operator đăng nhập test account → chạy lại probe (cookie `reddit_session`
  + `document.cookie` loid + `/api/me.json` → modhash) → capture `CreatePost`
  hoặc verify `/api/submit` bắn thật 1 post trên subreddit test.

## Plan Change Log

(Trống — spike hoàn tất một lần, không qua review loop.)

## Review Triage Log

`review: none` — spike nghiên cứu, không đổi production code.
