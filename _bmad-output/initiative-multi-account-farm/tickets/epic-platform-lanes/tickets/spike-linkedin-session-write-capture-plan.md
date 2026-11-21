---
type: feature
ticket: '1'
status: built
route: oneshot
risk: high
baseline_revision: 2c3a2ecb74f9d90f178f12350e5ec5c3ecd61634
---

# Spike 15.1 — LinkedIn session + write capture feasibility

## Verdict

**NEEDS_OPERATOR_LOGIN — chưa replayable.** Không chứng minh được endpoint, header, hay body shape vì dedicated browser không giữ session LinkedIn và `linkedin.com` không mở được (TCP reset) trên cả host lẫn browser. Không có request `voyager/api` nào để capture. Skeleton `_linkedin_post_vars.json` **không** được tạo — mọi shape bên dưới là giả định công khai, đánh dấu unverified, không phải capture.

Không phải `not-feasible`: pattern replay same-origin (cookie `li_at` + `csrf-token` từ `JSESSIONID`) cùng họ với X/FB vẫn là hướng đúng cho story 15.2, nhưng gate là mạng + login operator + một lần capture composer. Không tự login. Không đăng bài.

## Bằng chứng (live, CDP dedicated browser)

- `browser.ensure_running()` — browser đã chạy sẵn, CDP `127.0.0.1:9222`. Tab sống khác: `x.com/home`, Instagram, TikTok, Reddit. Không có tab LinkedIn trước probe.
- Cookie store (`Storage.getCookies` / `cdp.cookies("linkedin.com")`): **không** cookie domain `linkedin.com`. Không có `li_at`, không có `JSESSIONID`. `~/.agent-reach/browser/session.json` (~24KB) cũng **0** cookie LinkedIn. Không in giá trị cookie.
- `browser.open_url("https://www.linkedin.com/feed/")` rồi `Page.navigate` cùng URL trên tab đó: trang rơi `chrome-error://chromewebdata/`, title `www.linkedin.com`, body "This site can’t be reached / The connection was reset / ERR_CONNECTION_RESET". CDP trả `errorText: net::ERR_CONNECTION_RESET`. `document.cookie` trên trang lỗi ném `SecurityError` (không phải logout).
- Đối chứng host, không qua browser: `curl -4` và `curl -6` tới `https://www.linkedin.com/` và `/feed/` đều `Recv failure: Connection reset by peer` (IPv4 `104.18.41.41`, IPv6 Cloudflare). Cùng máy, `curl -4 https://x.com/` → **HTTP 200**. Reset là đường tới LinkedIn, không phải CDP chết hay máy mất mạng.
- Vì không có document LinkedIn: không probe được feed / nav profile / form login, không arm được interceptor `fetch`+XHR, không gọi GET vô hại trên `voyager/api`. Không click composer, không đăng gì.

## Đã chứng minh vs giả định

**Đã chứng minh:** không session (`li_at` vắng ở cookie store lẫn session đã lưu); trang LinkedIn không load trên browser dedicated; host cũng bị reset tới LinkedIn trong khi X thì được. Story 15.2 không thể live-verify trên máy này cho đến khi hai gate dưới thông.

**Giả định — unverified, KHÔNG dùng làm skeleton ship:**

Tài liệu công khai (client Voyager cũ, ví dụ thư viện `linkedin-api`) mô tả share creation là REST, không phải GraphQL của X/FB:

```
POST https://www.linkedin.com/voyager/api/contentcreation/normShares
credentials: 'include'          # li_at httpOnly tự gắn same-origin; đừng copy ra header
headers:
  csrf-token:                 # JSESSIONID bỏ dấu ngoặc kép; giá trị dạng ajax:...
  x-restli-protocol-version: 2.0.0
  accept: application/vnd.linkedin.normalized+json+2.1
  content-type: application/json; charset=UTF-8
  x-li-lang: en_US
body (historical, unverified):
  visibleToConnectionsOnly, externalAudienceProviders,
  commentaryV2: {text, attributes}, origin: "FEED",
  allowedCommentersScope: "ALL", postState: "PUBLISHED"
```

Endpoint ứng viên khác, cùng mức unverified, capture phải phân biệt: `POST /voyager/api/voyagerContentcreationDashShares`, `POST /voyager/api/voyagerSocialDashNormShares`. Web client đời mới có thể đã chuyển create-share sang `POST /voyager/api/graphql?action=execute&queryId=...`. Giả định ticket "REST chứ không GraphQL" **chưa được xác nhận** — story 15.2 phải để capture quyết định, không hardcode path trên.

Header có thể bắt buộc thêm và chỉ capture mới biết: `x-li-track`, `x-li-page-instance`, `x-restli-method`. `li_at` httpOnly nên `document.cookie` không thấy; session thật vẫn gửi được bằng `credentials:'include'` từ tab `linkedin.com`. `JSESSIONID` thường JS-readable (csrf); nếu build nào đánh httpOnly thì đọc tên+giá trị qua `cdp.cookies` ngay trước send, không ghi ra đĩa.

## Shape gợi ý cho story 15.2

Module `agent_reach/channels/_linkedin_write.py`, bám scaffold `_facebook_write.py` nhưng verb là REST fetch, không GraphQL `doc_id`:

- `prepare` qua `session_reuse` (platform `linkedin`) → mở/tìm tab `linkedin.com` (`cdp._json("/json/list")` + `open_url` / `Page.navigate`) → `Runtime.evaluate` fire-and-poll, result vào `window[key]` per call (giống `send_mutation` / `_eval`).
- Login gate, tách khỏi lỗi mạng:
  - `net::ERR_CONNECTION_RESET` hoặc `chrome-error` → `UPSTREAM_BROKEN` (mạng), không nhầm `NEED_LOGIN`.
  - Document load được nhưng không có `li_at` (CDP) và không có chrome feed (nav "Me" / share box; có form `session_key` hoặc Sign in) → `NEED_LOGIN`. Không tự điền form.
- Header mint **live mỗi send**: `csrf-token` từ `JSESSIONID`. Hằng chỉ sau khi capture xác nhận (`x-restli-protocol-version`). Không persist `li_at`.
- Skeleton một lần, file `agent_reach/channels/_linkedin_post_vars.json`, cùng ý `_fb_post_create_vars.json`: `{method, path, headers_template, body_template}` đã sanitize (bỏ csrf thật, member urn, text thật). Path/query id nếu có thì đưa vào `query-ids` hot-reload, không hardcode trong module. File này **chưa tồn tại** — đừng tạo từ shape unverified ở trên.
- Ledger: ghi qua `WRITE_ACTIONS` như story 12.7, chỉ sau post thật trên **test account**. Session personal của operator → không đăng để "thử capture".
- Channel `linkedin.py` hiện chỉ job search/read (`OpenCLISiteChannel`, `login_cookie = "li_at"`). Write là action mới; đừng nhét post vào public-web job lane.

## Việc operator phải làm trước 15.2

1. Mở đường tới LinkedIn trên chính máy này. Tiêu chí: `curl -I https://www.linkedin.com/` ra HTTP status, không `connection reset`. `x.com` đã 200 nên đây là path/edge LinkedIn (Cloudflare reset cả v4 lẫn v6), không phải browser hỏng. Đừng nhờ agent né reset.
2. Trong **dedicated browser** của Agent Reach (không phải Chrome cá nhân), mở `https://www.linkedin.com/login` và tự đăng nhập. Ưu tiên account test. Không đưa mật khẩu cho agent. Xong khi cookie store có `li_at` và `/feed/` render share box.
3. Capture lại: arm interceptor `fetch`+XHR (gắn bằng `Runtime.evaluate` trên page đã load — `Page.addScriptToEvaluateOnNewDocument` đã fail main-world trên CloakBrowser ở spike X), đăng **một** bài từ composer trên test account, lưu skeleton đã sanitize. Nếu session là account cá nhân thật → dừng, đánh dấu `pending live capture`, không đăng.

## Rủi ro

- Body/path Voyager đổi theo build; ship shape unverified sẽ 4xx hoặc đăng nhầm bề mặt.
- `csrf-token` lệch `JSESSIONID` (quên strip quote) → 403, dễ bị đọc nhầm thành logout.
- Reset mạng hiện tại che mất mọi probe; sửa login trước khi sửa route là vô ích.
- Đăng lên account operator để lấy capture là ngoài scope spike này và ngoài story cho đến khi có test account.
