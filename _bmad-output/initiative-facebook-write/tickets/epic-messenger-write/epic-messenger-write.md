---
id: 2
type: epic
title: "Messenger send"
parent: initiative-facebook-write
covers: ["R5", "R6", "R7"]
sources: []
companions: []
after: []
refined: true
risk: high
---

# Messenger send

## Description

Chứng minh (hoặc bác) khả năng gửi Messenger DM bằng replay request từ dedicated browser: web messenger có thể dùng GraphQL mutation hoặc transport riêng (MQTT/LSPlatform, sync sequence). Nếu replayable → action `facebook message`; nếu không → descope có lý do và chỉ đường fallback (browser automation).

## Outcome

Agent gửi được tin nhắn Messenger qua CLI — hoặc người dùng có câu trả lời dứt khoát tại sao không, với hướng thay thế.

## Requirements

- R1 (parent R5): nếu transport replayable, `facebook message <user-or-thread> "text"` gửi DM và tin nhắn xuất hiện trong thread trên facebook.com.
- R2 (parent R6): nếu ship, send action đáp ứng cùng chuẩn với các write verbs khác — live tokens, typed ReachError, verify được, không duplicate.
- R3 (parent R7): docs phản ánh đúng kết quả — mô tả verb `message` + giới hạn nếu ship, hoặc ghi descope + lý do kỹ thuật + fallback nếu không.

## Constraints

- Spike trước, implement sau: không commit action `message` khi transport chưa chứng minh.
- Chỉ gửi DM text; không media, không group management.
- Dùng lại capture harness và replay helpers từ epic-facebook-write-actions.

## Done when

- Verdict transport được viết rõ (mutation/doc_id nếu replayable, hoặc bằng chứng non-replayable).
- Nếu replayable: `facebook message` chạy live với message verify trong thread.
- Nếu không: docs ghi descope + lý do + hướng fallback.
- Delivery: kết quả merge vào `main` của fork và push lên origin (self-hosted tool — shipped = code trên main).

## Boundaries

- Sibling epic-facebook-write-actions sở hữu: mọi write verb khác + capture harness (epic này consume).
- Out: group chat management, media trong DM, read/sync thread (đó là read surface khác), Marketplace messages.

## References

- parent — `_bmad-output/initiative-facebook-write/initiative-facebook-write.md`, section Requirements
- code — `agent_reach/channels/_facebook_write.py` (replay pattern sẽ tái dùng nếu transport tương thích)

## Notes

- Assumption: spike có thể chứng minh web send là một request HTTP replayable; nếu là MQTT/LSPlatform sync-sequence thì descope (implement MQTT client vượt scope agent-reach).
- Decision: 2026-11-21 — tracer bullet = Story 1 spike (hitl) vì transport là unknown gating mọi thứ sau; Story 2 chỉ build khi spike ra verdict replayable, Story 3 docs/descope chạy theo verdict.
- Decision: 2026-11-21 — R7 (parent) được chia: phần write verbs thuộc epic-facebook-write-actions story 1.6, phần messenger verb hoặc descope thuộc story 2.3 ở epic này.
- Waits on story 1.1 (capture harness) vì cần nó để lấy body send-request thật; không chờ cả epic-facebook-write-actions nên spike chạy song song được với các story 1.2–1.5.
