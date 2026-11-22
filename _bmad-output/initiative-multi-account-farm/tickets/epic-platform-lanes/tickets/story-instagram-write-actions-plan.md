---
title: 'Story 15.6 — Instagram post write action (comment + like lane)'
type: feature
ticket: '6'
created: '2026-11-22'
status: built
baseline_revision: eb925a2
route: full
route_source: pinned
risk: medium
review: none
review_source: pinned
lenses_ran: []
context: []
---

# Story 15.6 — Instagram write lane

## Scope decision (explicit)

Spike 15.5 kết luận `instagram post` = media pipeline hai bước
(`PUT /rupload_igphoto/` rồi `POST /api/v1/media/configure/`); IG không có
text-only post. **Scope lựa chọn: (a) descope `post`** — action `post` tồn tại
nhưng raise `BAD_INPUT` với message nêu rõ hai endpoint và lý do; verb thật
ship là `comment` + `like`/`unlike`, replay form API của IG web:

- `comment` → `POST /api/v1/web/comments/<media_id>/add/` (form `comment_text`)
- `like`/`unlike` → `POST /api/v1/web/likes/<media_id>/like|unlike/` (body rỗng)

Header giống XHR thật quan sát được trong page: `X-CSRFToken` (cookie
`csrftoken`), `X-Requested-With: XMLHttpRequest`, `X-IG-App-ID:
936619743392459`. Auth = session cookie qua `credentials:"include"`.

## Design

- Mirror `_reddit_write`: fire-and-poll `fetch` trong page (không await) +
  per-call window key + `_sent` sentinel chống double-post khi CDP timeout.
- `media_id`: accept URL `/p|reel|reels|tv/<code>/`, bare shortcode, hoặc
  numeric id. Shortcode↔media_id decode deterministic (alphabet
  `A-Za-z0-9-_`, base64) — verify live: `/p/Df5AG_slAwS/` decode đúng
  `4033255421997485074` khớp `"media_id"` nhúng trong HTML post page. Không
  cần lookup request nào.
- Session gate: jar phải có `sessionid` + `ds_user_id` + `csrftoken`, thiếu →
  `NEED_LOGIN` (hint `agent-reach login instagram`).
- `classify_ig`: 429 → `RATE_LIMITED`; 401/403 → `NEED_LOGIN`; ≥500 →
  `UPSTREAM_BROKEN`; body HTML login wall → `NEED_LOGIN`;
  `checkpoint_required`/`challenge_required`/`feedback_required`/`spam` →
  `PLATFORM_BLOCKED` (terminal, operator phải giải checkpoint); "Please
  wait"/`rate_limited`/`try again later` → `RATE_LIMITED`; 404/"not found" →
  `BAD_INPUT`; còn lại → `UPSTREAM_BROKEN`.
- Ledger/quota: `WRITE_ACTIONS["instagram"] = {comment, like, unlike}`;
  `_ACTION_CLASS` map `like`→`react` (cap 200/ngày của `DEFAULT_CAPS`) như
  spec; `unlike` giữ class riêng uncapped (giống fb `unreact`); `comment` →
  class `comment` (60/ngày).

## Files

- `agent_reach/channels/_instagram_write.py` — lane
- `agent_reach/channels/instagram.py` — Actions `comment`/`like`/`unlike`/`post`
- `agent_reach/runtime/dispatch.py` — `WRITE_ACTIONS["instagram"]` +
  `_ACTION_CLASS`/`_write_class`
- `tests/test_instagram_write.py`
- `agent_reach/skill/references/capabilities.md`, `SKILL.md`, `SKILL_en.md`

## Verify

```bash
uv run --extra dev python -m pytest tests/test_instagram_write.py -q
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- Lane CLI end-to-end chạy được: `agent-reach instagram like
  https://www.instagram.com/p/Df5AG_slAwS/` → pipeline prepare → mở lại
  dedicated browser (đã tắt) → tab instagram.com → cookie gate →
  **`NEED_LOGIN`** đúng message. `instagram comment <url> "text"` → cùng
  `NEED_LOGIN`. `instagram post` → `BAD_INPUT` descope message đúng.
- **Live post-confirm (comment id / like ok) CHƯA chứng được**: session đã
  chết trong lúc build. Lúc probe đầu jar có `sessionid` (77 ký tự,
  ds_user_id `14519691243` = epsiloncryptoai) nhưng server trả login wall:
  `/api/v1/accounts/current_user/` trả HTML login, `CurrentUserInitialData
  .USER_ID="0"`, `GET /api/v1/media/<id>/info/` → 401 `require_login`.
  CAA wall "Continue as epsiloncryptoai" hiện; click trusted vào Continue
  mở `CAAFetaAYMHPasswordDialog` (đòi password). ~07:08 `sessionid` bị IG
  xóa khỏi jar; ~07:19 browser restart, jar vẫn không có `sessionid`. Login
  cần password — việc của operator (`agent-reach login instagram`), không
  phải agent.
- Shortcode decode verify live trước khi mất session: `/p/Df5AG_slAwS/` →
  `4033255421997485074` khớp `media_id` trong page HTML.
- Tests: `tests/test_instagram_write.py` 12 passed. Full suite **1107
  passed** (1095 baseline + 12).
- Deviations: không comment/like thật được (NEED_LOGIN), nên không có
  comment id/permalink và cũng không cần cleanup. Shape endpoint
  (`/api/v1/web/comments/<mid>/add/`, `/api/v1/web/likes/<mid>/like|unlike/`)
  chưa được re-confirm logged-in — POST thử lúc logged-out trả 404 HTML
  login wall (đúng phân loại). Chạy lại live verify sau khi operator đăng
  nhập.
