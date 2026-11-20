---
id: 5
type: epic
title: "Platform lanes mở rộng"
parent: initiative-multi-account-farm
covers: ["R2"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: high
---

# Platform lanes mở rộng

## Description

Nhân write lane sang các platform còn lại theo thứ tự ưu tiên farm đã chốt: **LinkedIn → Reddit → Instagram**. Mỗi lane dùng lại mutation capture pipeline + doc-id config + replay pattern đã chứng minh trên X (epic 2). TikTok xử lý riêng: repo chưa có cả read channel — story đầu là capture spike, khả thi thì build, không thì descope có ghi lý do.

## Outcome

Farm post được đa platform theo cùng 1 cơ chế, thêm platform mới = việc lặp lại có quy trình chứ không phải dự án mới.

## Done when

- Write actions core (post tối thiểu, reply/comment nếu platform cần) verify live trên designated test account cho từng platform: LinkedIn, Reddit, Instagram — test account + session login sẵn do operator cung cấp (provisioning Out of scope); **story đầu mỗi lane = verify login-state trước khi capture mutation**.
- Mỗi lane có doc-ids trong config hot-reloadable, capture theo pipeline chuẩn, write ghi registry.
- TikTok: spike kết luận khả thi → lane được build, hoặc descope ghi rõ lý do trong Notes.

## Boundaries

- Out: Facebook (initiative-facebook-write), caps/kill-switch enforce (epic-account-guardrails — lanes này chịu enforcement như mọi write), XActions fallback (epic-farm-resilience).
- Dependencies authoritative: `tickets/tickets.toml`.
- Must not change: replay pattern contract của epic 2; mỗi lane write phải đi qua enforcement path.
- Gate giống epic 2: live verify chỉ trên designated test account không nằm trong farm rotation.
- Touch point: capture pipeline + doc-id config (epic 2 tạo), registry (epic 1), channels mới `agent_reach/channels/`.

## References

- parent — `initiative-multi-account-farm.md`, Requirement R2
- architecture — `docs/multi-account-farm-architecture.md` §4a item 2, §5 (thứ tự ưu tiên)
