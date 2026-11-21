---
title: 'Story 15.4 — Reddit post + comment write actions'
type: feature
ticket: '4'
created: '2026-11-22'
status: built
baseline_revision: 2e42bf4
route: full
route_source: pinned
risk: medium
review: none
review_source: pinned
lenses_ran: []
context: []
---

# Story 15.4 — Reddit post + comment write actions

## Goal

Write lane cho Reddit trong dedicated browser: `post` (text hoặc link) và
`comment`, replay `POST /api/submit` và `POST /api/comment` với modhash lấy
mới mỗi lần từ `GET /api/me.json`. Không OAuth bearer. Đăng ký `WRITE_ACTIONS`
để quota và ledger áp dụng.

## Design

- Cùng shape fire-and-poll với Facebook/X: `fetch` không await, kết quả nằm
  ở window key riêng, tránh double-post khi CDP timeout.
- `sr`: `r/test` → `test`, `u/name` → `u_name`. Có `url` thì `kind=link`,
  không thì `kind=self` (bắt buộc `text`).
- Lỗi: `RATELIMIT` → `RATE_LIMITED`; 401/403/`USER_REQUIRED`/không modhash →
  `NEED_LOGIN`; `SUBREDDIT_NOEXIST`/`NO_TEXT`/validation → `BAD_INPUT`; còn
  lại (`BAD_CAPTCHA` included) → `UPSTREAM_BROKEN`.
- `reddit_delete` (`POST /api/del`) là helper dọn rác, không phải channel
  action, không vào `WRITE_ACTIONS`.
- GraphQL `CreatePost` không làm: spike ghi là fallback, nhưng không có
  persisted-query hash, và `gql.reddit.com` / `oauth.reddit.com` từ page
  trả `Failed to fetch`.

## Files

- `agent_reach/channels/_reddit_write.py`
- `agent_reach/channels/reddit.py` — action `post`, `comment`
- `agent_reach/runtime/dispatch.py` — `WRITE_ACTIONS["reddit"]`
- `tests/test_reddit_write.py`
- `agent_reach/skill/references/capabilities.md`, `SKILL.md`, `SKILL_en.md`

## Verify

```bash
uv run --extra dev python -m pytest tests/test_reddit_write.py -q
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- Session: dedicated browser không lắng nghe 9222 lúc bắt đầu; `prepare_reddit`
  mở lại profile owned. `GET /api/me.json` → `name=Objective_Bet_368`,
  modhash length 50, HTTP 200.
- Live post `r/test`, title `agent-reach write-lane probe 025225`, body
  `automated probe, safe to delete`: HTTP 200, `errors=[["BAD_CAPTCHA",
  "That was a tricky one. Why don't you try that again.", "captcha"]]`.
  Không có `url` — không tạo post.
- Fallback `u/Objective_Bet_368`: cùng `BAD_CAPTCHA`. Thử lại từ origin
  `old.reddit.com`: cùng body. Không retry thêm.
- Fallback cross-origin (`oauth.reddit.com/api/submit`, `gql.reddit.com`)
  từ page: `TypeError: Failed to fetch`. `GET /svc/shreddit/token` → 404.
  Không có post URL, không có comment id, không có gì để `POST /api/del`.
- Tests: `tests/test_reddit_write.py` 9 passed. Full suite **1084 passed**.
- Deviations: lane tới được Reddit và phân loại đúng, nhưng account/phiên này
  bị legacy API chặn captcha nên chưa có URL post thật. GraphQL fallback
  không ship (không có doc-id, cross-origin không fetch được từ page).
