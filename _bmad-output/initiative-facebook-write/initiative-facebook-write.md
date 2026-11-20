---
type: initiative
title: "Facebook write actions"
sources: []
companions: []
after: []
risk: medium
---

# Facebook write actions

## Description

Agent-Reach hiện là read-first tool; fork `deptrai/Agent-Reach` đã có ngoại lệ write đầu tiên (`facebook post`, commit `9d663fd` + `19dbf3b`). Initiative này hoàn thiện write surface cho facebook: reaction, comment, post edit/delete, share và Messenger — tất cả theo pattern đã chứng minh (replay mutation `/api/graphql` thật trong dedicated browser, live tokens từ `window.require`).

## Outcome

Người dùng agent thực hiện được các thao tác write phổ biến trên Facebook qua một lệnh `agent-reach facebook <verb>`, không cần Puppeteer, mỗi kết quả verify được trên facebook.com thật.

## Requirements

- R1: `facebook react` đặt/gỡ reaction (like/love/haha/wow/sad/angry) trên post hoặc comment URL; trạng thái reaction hiện trên facebook.com.
- R2: `facebook comment` tạo comment trên post URL và xoá được comment của mình; comment xuất hiện/biến mất trên post thật.
- R3: `facebook post edit` và `facebook post delete` sửa/xoá post của chính mình qua story id hoặc permalink.
- R4: `facebook share` share một post lên timeline của mình với text và audience tuỳ chọn; story mới xuất hiện trên timeline.
- R5: `facebook message` gửi DM Messenger — chỉ khi web transport là request replayable; spike quyết định, nếu không thì descope có ghi rõ lý do.
- R6: Mọi write action replay mutation `/api/graphql` thật trong dedicated browser với live tokens; lỗi là `ReachError` có type (NEED_LOGIN/RATE_LIMITED/UPSTREAM_BROKEN/BAD_INPUT); kết quả verify được (permalink/id); fire-and-poll, không blind retry.
- R7: SKILL.md + README mô tả đúng write surface thực tế và safety defaults (own-timeline, audience mặc định only_me khi áp dụng).

## Constraints

- Replay pattern từ `agent_reach/channels/_facebook_write.py`: capture body request thật → skeleton JSON → replay với tokens sống; không Puppeteer.
- Chỉ reuse session trong dedicated agent-reach browser; không lưu hay xuất credentials.
- Write surface giới hạn ở channel `facebook` trong initiative này; các channel khác giữ read-only.
- Mọi write dùng fire-and-poll result; cấm blind retry (tránh duplicate — bài học từ review của post action).

## Done when

- `agent-reach facebook` có và verify live cho: react, comment create/delete, post edit/delete, share — mỗi action chạy ít nhất một lần trên tài khoản thật với artifact kiểm chứng được.
- Messenger send hoặc hoạt động end-to-end, hoặc được ghi tài liệu descope kèm lý do kỹ thuật.
- Docs cập nhật khớp thực tế; `pytest` suite xanh; `agent-reach check facebook` pass.
- Delivery: tất cả work merge vào `main` của fork `deptrai/Agent-Reach` và push lên origin (self-hosted tool — shipped = code trên main).

## Boundaries

- Out: upload ảnh/video (multipart pipeline, độ phức tạp riêng), post lên page/group (composer surface khác), friend/group membership (opencli đã cover `add-friend`/`join-group`), bulk operations, scheduling.
- Touch point: `agent_reach/channels/_facebook_write.py`, `facebook.py`, `_facebook_vars.json`, `tests/test_facebook_write.py` — owner: epic-facebook-write-actions.
- Must not change: hành vi read actions (search/profile); mô hình auth qua dedicated browser; các channel khác.
- Tracer path: capture harness → react (đơn giản nhất) → comment → post ops → share → docs → sweep; messenger chạy song song sau khi có harness.

## References

- parent — none
- code — `agent_reach/channels/_facebook_write.py` (replay pattern, error mapping, `_POST_JS`)
- code — `agent_reach/channels/_facebook_vars.json` (variables skeleton, sanitized)
- code — `agent_reach/channels/facebook.py` (channel actions, `login_cookie`)
- code — `agent_reach/runtime/dispatch.py` (action/backends, browser slot pacing)

## Notes

- Assumption: tất cả mutations cần thiết là replayable POST như `ComposerStoryCreateMutation`; Messenger có thể phá giả định này (spike sẽ trả lời).
- Open question: upload ảnh/video và page/group posting — deferred, mở epic riêng nếu có demand.
