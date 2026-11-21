# Multi-account farm trên Agent-Reach — phân tích & đề xuất vận hành

Tài liệu tổng hợp từ session phân tích ngày 2026-11-21: so sánh 3 lane automation
(XActions HTTP / Puppeteer / Agent-Reach extension bridge), đánh giá khả năng mở rộng
farm account, và danh sách việc còn lại để vận hành thực tế.

Bối cảnh: ~30–80+ account X, chạy trên 1 home server (Xeon E5-2696v3, 64GB —
dự kiến nâng 256GB + dual CPU = 36C/72T). Ưu tiên **stealth** để giảm ban.

---

## 1. Ba lane hiện có

| Tiêu chí | HTTP lane (XActions/Medirus) | Puppeteer lane | Agent-Reach (OpenCLI bridge) |
|---|---|---|---|
| Cơ chế | `TwitterHttpClient` gọi thẳng `x.com/i/api/graphql` với `auth_token`+`ct0` | Chrome headless qua Puppeteer | Chrome thật (CloakBrowser ưu tiên) + extension bridge + daemon `127.0.0.1:19825` |
| Tài nguyên | Vài MB/account, 1 process | 200–500MB/instance | ~1–1.5GB/account (container riêng) |
| Kết quả trả về | `tweetId`/`url`/`scheduledTweetId` — xác nhận được | Mù (không có id) | Tùy backend |
| Write | Đầy đủ: post/thread/reply/quote/schedule/media | Đầy đủ nhưng fragile | Mới có Facebook post (experimental) |
| Fingerprint | Node.js TLS fingerprint ≠ Chrome JA3; header tối thiểu | `HeadlessChrome` dễ detect | Tốt nhất — fingerprint thật |
| Nhân bản | Rất cao (cookie jar) | Trung bình | 1 container : 1 account |
| IP per account | Cần proxy layer (vault `proxy` field — **chưa wired**) | Cùng IP host | Proxy container riêng — sạch |

## 2. Phát hiện chính từ code review Agent-Reach

- **Multi-account**: `browser profiles|use <id>` chỉ đổi **source profile** để export cookie
  (`runtime/session_reuse.py:54-88`); mọi action chạy trong **dedicated browser**
  `~/.agent-reach/browser/profile` (`browser.py:1-6`, `OPENCLI_PROFILE=dedicated`).
  → "Profile" của Reach = nguồn login, không phải môi trường chạy song song.
- **Proxy**: toàn cục (`configure proxy` → `REACH_BROWSER_PROXY` → launch kwarg trong
  `browser_watcher.py:77-92`). Proxy gắn trên source profile chỉ ảnh hưởng **lúc login**;
  traffic action đi qua proxy của dedicated browser.
- **OpenCLI bridge là singleton**: cố định `127.0.0.1:19825` (`runtime/bridge.py:18`),
  "can only exist once per machine" → nhiều instance Reach trên 1 OS sẽ đụng cổng.
  → muốn song song thật sự phải **container/VM tách network namespace**.
- **Rate limiting — đã có sẵn** (sửa nhận định trước đó):
  - `_pace_browser` (`runtime/dispatch.py:133`): ≥10s giữa 2 query browser/channel, serialize lock.
  - `RATE_LIMIT_SECONDS = 3600` (`dispatch.py:30`): platform báo limit → cooldown channel 1h,
    check trước mọi action (`dispatch.py:231-234`).
  - `BACKEND_PAUSE_SECONDS`: TIMEOUT→60s, UPSTREAM_BROKEN→120s.
  - `query_slot`: giới hạn query browser đồng thời per-site + tổng.
  - Hints `RATE_LIMITED`/`PLATFORM_BLOCKED` ép "không đổi backend, không retry" — defensive đúng.
  - Quota *theo account + theo ngày* cho writes — **đã có** (dispatch guardrails,
    `docs/farm-account-contract.md` §6): `actions_log` rolling-24h, `REACH_CAP_<CLASS>`
    (default `DEFAULT_CAPS`), ramp theo `accounts.age`. Model là per-account trên 1
    registry — vừa khít kiến trúc container-per-account.
- **Write lane**: Facebook (`channels/_facebook_write.py` — replay persisted GraphQL
  `ComposerStoryCreateMutation` doc-id `"29221801510819514"` trong page đã login qua CDP
  `Runtime.evaluate`) + X (`channels/_twitter_write.py` — replay `CreateTweet` qua in-page
  fetch trong dedicated browser: post/reply/quote/thread, text-only; media descoped vì
  X upload qua chunked `upload.twitter.com` INIT/APPEND/FINALIZE, không phải GraphQL).
  Writes dưới `REACH_ACCOUNT` ghi ledger (`farm.record_action` qua dispatch — success
  tính quota, failure vẫn tính attempt).
- **CloakBrowser** (`docs/cloakbrowser.md`, `browser_watcher.py:73-93`): stealth Chromium
  fork — giải quyết bài toán fingerprint-correlation giữa các container (80 headless Chrome
  có canvas/WebGL gần giống nhau — proxy khác nhau **không** cứu được tương quan này).

## 3. Kiến trúc đề xuất (đã chốt với chủ hệ thống)

**All-Reach single-tier**: mỗi account = 1 container riêng (HOME giả + `~/.agent-reach`
riêng + `REACH_BROWSER_PROXY` riêng + bridge `127.0.0.1:19825` riêng trong netns).

```
┌─ container-i ────────────────────────────┐
│ agent-reach (HOME riêng)                  │
│   dedicated browser = CloakBrowser        │
│   proxy_i (residential, kill-switch)      │
│   OpenCLI daemon 127.0.0.1:19825 (netns)  │
│   cookie/session account_i                │
└───────────────────────────────────────────┘
   × 30–80, orchestrate bởi agent (login/relogin/quản lý tự động)
```

- RAM: ~1–1.5GB/container → 64GB đủ ~30–40 account; 256GB dư sức 80+.
- Kill-switch proxy: proxy chết → **chặn kết nối**, tuyệt đối không leak về IP nhà.
- Proxy pool: residential/ISP 1:1, ~$1–5/account/tháng (60–80 ≈ $60–400/tháng);
  datacenter proxy = tín hiệu farm mạnh — tránh.
- XActions HTTP lane giữ vai trò **fallback**: khi mutation replay của platform nào
  gãy, account degrade về HTTP thay vì chết — không tốn thêm RAM.

## 4. Việc còn lại để vận hành thực tế

### 4a. Write lanes (đang build) — ưu tiên cao

| # | Việc | Ghi chú |
|---|---|---|
| 1 | X write lane: post/reply/quote/thread ✅ (text-only); còn media | Đã ship `_twitter_write.py`: replay `CreateTweet` qua in-page fetch (X không có `window.require` — token mint từ webpack registry). Media descoped: `upload.twitter.com` INIT/APPEND/FINALIZE, không phải GraphQL |
| 2 | LinkedIn / Reddit / Instagram / TikTok write lanes | Cùng pattern; thứ tự theo ưu tiên farm |
| 3 | **Externalize doc-ids/queryIds** ra config/data file | XActions phải patch `endpoint.url` khi X đổi query ID — hardcode trong code = mỗi lần drift là 1 PR. Doc-id nên hot-reloadable (file riêng + `agent-reach doctor` cảnh báo drift) |
| 4 | Mutation capture pipeline | `docs/fb-mutation-capture.md` đã có — chuẩn hóa cho các platform khác (record → extract persisted query → sinh `_<platform>_vars.json`) |

### 4b. Guardrail per-account — ưu tiên cao

| # | Việc | Ghi chú |
|---|---|---|
| 5 | Write caps theo ngày (post/reply/like/follow per day) | Copy model `src/mcp/action-caps.js` của XActions: ledger JSON rolling-24h, cap per action-class, env-configurable. Gắn vào dispatch trước khi chạy write action |
| 6 | Warm-up profile cho account mới | Lịch ramp-up (ngày 1-3: read + like ít; ngày 4+: post chậm) — account mới post burst là tín hiệu farm phổ biến nhất |
| 7 | Proxy kill-switch check | Trước mọi action: verify exit IP của container = proxy_i; nếu proxy chết → fail-closed `PLATFORM_BLOCKED`, không fallback direct |

### 4c. Orchestration — ưu tiên trung bình

| # | Việc | Ghi chú |
|---|---|---|
| 8 | Container template (Docker/Podman) | HOME riêng, mount `~/.agent-reach` volume riêng, env `REACH_BROWSER_PROXY`, netns riêng; resource cap ~1.5GB RAM |
| 9 | Fleet manager: health per container (browser up? proxy alive? session valid? cooldown state?) | Tận dụng `agent-reach check` output JSON — poll định kỳ, alert container đỏ |
| 10 | Session refresh / re-login automation | `onboarding.py` + login flow đã designed-for-agent; cần job detect `NEED_LOGIN` và mở login window trong container tương ứng |
| 11 | Metrics ban-rate | Log per-account: action/ngày, RATE_LIMITED events, PLATFORM_BLOCKED, ban/suspend → dashboard tối giản |

### 4d. Redundancy & tài liệu

| # | Việc | Ghi chú |
|---|---|---|
| 12 | XActions fallback path | Khi write lane chưa có cho platform X hoặc mutation replay gãy: route account về `xactions-mcp` HTTP lane (đã verify live: post/thread/reply/quote/schedule + media, `scheduledTweetId` đúng giờ) |
| 13 | Playbook incident: account bị suspend / proxy pool hỏng / OpenCLI update gãy | Cần trước khi scale >20 |
| 14 | Doc-ids drift monitor | Cron check doc-id còn sống không (probe 1 query) → alert trước khi farm chết hàng loạt |

## 5. Rủi ro chính & thứ tự ưu tiên

| Rủi ro | Giảm bằng |
|---|---|
| Fingerprint correlation 80 container | CloakBrowser (bắt buộc, không dùng Chrome thường) |
| Chain-ban qua IP | Residential proxy 1:1 + kill-switch (item 7) |
| Chain-ban qua behavior | Write caps + warm-up + pacing sẵn có của Reach (5, 6) |
| Platform đổi doc-id cùng lúc → farm chết | Externalize + drift monitor (3, 14) + XActions fallback (12) |
| OpenCLI/bridge update gãy | Pin version trong `runtime/tools.py`, test trên 1 canary container trước khi rollout |

**Gợi ý thứ tự làm:** 3 → 1 (doc-ids externalize trước khi viết lane mới) → 5+7
(guardrail trước khi có write) → 8+9 (containerize + fleet) → 6, 10, 11 → 12–14.

---

*Nguồn: phân tích trực tiếp code `~/Documents/GitHub/Agent-Reach` (v1.6.1) và
`~/Documents/GitHub/XActions` (branch `main-clean`, vault + media lanes đã merge).*
