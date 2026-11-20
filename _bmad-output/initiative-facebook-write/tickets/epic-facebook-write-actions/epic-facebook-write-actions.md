---
id: 1
type: epic
title: "Facebook write actions"
parent: initiative-facebook-write
covers: ["R1", "R2", "R3", "R4", "R6", "R7"]
sources: []
companions: []
after: []
refined: true
risk: medium
---

# Facebook write actions

## Description

Mở rộng `facebook post` đã có thành bộ write verbs cho tương tác trên Facebook: react/unreact, comment create/delete, post edit/delete và share. Mỗi verb là một `Action` trong `FacebookChannel`, replay mutation `/api/graphql` đã capture qua CDP trong dedicated browser — cùng pipeline với `post`: capture harness → skeleton vars → replay action → unit tests → verify live.

## Outcome

Agent thực hiện được các tương tác write phổ biến nhất trên Facebook (react, comment, sửa/xoá bài của mình, share) bằng CLI, với typed errors và kết quả kiểm chứng được — không cần Puppeteer hay credential export.

## Requirements

- R1 (parent R1): `facebook react <post-or-comment-url> --type <reaction>` đặt reaction; `facebook unreact <url>` gỡ; đủ 6 loại reaction của web.
- R2 (parent R2): `facebook comment <post-url> "text"` tạo comment; delete comment của mình qua id/URL.
- R3 (parent R3): `facebook post edit`/`delete` trên post của chính mình qua story id hoặc permalink; delete yêu cầu xác nhận rõ (guard trong action hoặc flag).
- R4 (parent R4): `facebook share <post-url> [--text ...] [--audience ...]` lên timeline mình; audience mặc định `only_me`.
- R5 (parent R6): mọi verb replay mutation thật với tokens sống; `ReachError` typed (NEED_LOGIN/RATE_LIMITED/UPSTREAM_BROKEN/BAD_INPUT); trả permalink/id verify được; fire-and-poll, không blind retry.
- R6 (parent R7): SKILL.md (vi + en) và README mô tả đúng write surface; gỡ hẳn wording "single write exception".

## Constraints

- Theo `_facebook_write.py`: skeleton vars từ capture thật, sanitize trước khi commit (không actor_id/uuid thật).
- Mỗi mutation mới có skeleton + doc_id riêng; doc_id rotate → re-capture là quy trình documented.
- Delete/edit chỉ tác động đối tượng của chính mình; default an toàn (audience `only_me` nơi áp dụng).
- Không thay đổi read actions search/profile.

## Done when

- `agent-reach facebook react|comment|post edit|post delete|share` chạy live được với artifact verify trên facebook.com.
- Capture harness cho phép lấy skeleton của một mutation bất kỳ trong ~1 lượt thao tác.
- Unit tests cho từng action mới (pattern `test_facebook_write.py`); suite xanh.
- Docs cập nhật; không còn mâu thuẫn read-only.
- Delivery: tất cả merge vào `main` của fork và push lên origin (self-hosted tool — shipped = code trên main).

## Boundaries

- Sibling epic-messenger-write sở hữu: DM send + transport spike của Messenger.
- Out: media upload, page/group composer, bulk ops — như initiative Boundaries.
- Touch point: `_facebook_write.py` family + `facebook.py` + tests — owner là epic này.

## References

- parent — `_bmad-output/initiative-facebook-write/initiative-facebook-write.md`, section Requirements
- code — `agent_reach/channels/_facebook_write.py`
- code — `agent_reach/channels/_facebook_vars.json`
- code — `agent_reach/channels/facebook.py`
- code — `tests/test_facebook_write.py` (harness pattern mới)

## Notes

- Decision: 2026-11-21 — tracer bullet = Story 1 "Mutation capture harness" (hitl) vì nó chứng minh pipeline capture→skeleton mà mọi story sau dùng; thứ tự sau đó theo độ rủi ro tăng dần; Story cuối = Refactor sweep.
- Decision: 2026-11-21 — Messenger tách epic riêng vì transport chưa chắc (có thể MQTT/LSPlatform chứ không phải GraphQL replay); nếu spike fail thì descope không ảnh hưởng epic này.
- Decision: 2026-11-21 — comment edit không đưa vào scope (giá trị thấp, thêm một mutation); có thể mở lại sau.
