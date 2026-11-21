---
id: 12
type: epic
title: "Write lanes replay mutation"
parent: initiative-multi-account-farm
covers: ["R1", "R3", "R4"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: true
risk: high
---

# Write lanes replay mutation

## Description

Đưa write surface của Agent-Reach lên X trước, theo replay pattern `_facebook_write.py` đã chứng minh: capture mutation thật → skeleton params → replay trong dedicated browser với live tokens. Nền móng là externalize persisted doc-ids/queryIds ra config hot-reloadable — X đổi doc-id thì sửa file chứ không sửa code. Các platform còn lại tách sang epic-platform-lanes.

## Outcome

Người vận hành farm gọi `agent-reach x post|reply|quote|thread` (kèm media nếu pattern cho phép) và verify được kết quả trên platform thật; thêm platform mới = chạy capture pipeline + thêm config.

## Done when

- `agent-reach x post|reply|quote|thread` (+ media nếu pattern cho phép) verify live được trên X từ host với env_i/proxy_i theo contract epic-account-substrate (proof "từ trong container riêng" nằm ở Done-when epic-fleet-orchestration).
- Doc-ids/queryIds của **toàn bộ persisted queries X đang dùng — kể cả read** (cùng một rotation giết luôn read surface nếu hardcode) — nằm trong config/data file hot-reloadable; `agent-reach doctor` (hoặc tương đương) cảnh báo được drift; probe cùng 1 doc-id từ ≥2 account cho kết quả giống nhau trước khi chốt "global theo platform" (nếu khác → config/monitor phải per-account-variant).
- Mutation capture pipeline tổng quát hóa từ `docs/fb-mutation-capture.md` dùng được cho ≥2 platform (FB đã có + X).
- Mọi write ghi vào registry của epic 11 (account nào, lúc nào, action gì).

## Boundaries

- Out: lane platform khác (epic-platform-lanes), guardrails (epic-account-guardrails), container/fleet (epic-fleet-orchestration), XActions fallback (epic-farm-resilience), Facebook write surface (initiative-facebook-write).
- Drift delineation: epic này = on-demand config check (`doctor`); proactive probe + alert là của epic-farm-resilience.
- Dependencies authoritative: `tickets/tickets.toml`.
- Gate: live verify chỉ trên **designated test account không nằm trong farm rotation** — account chỉ được vào rotation sau khi epic 13 Done-when.
- Tracer/spike bắt buộc là story đầu: chứng minh X mutation capture được (x-client-transaction-id/bearer/ct0 có lộ qua trang không như DTSG/LSD của FB chưa ai verify). Nếu không khả thi → XActions HTTP lane thăng làm lane chính thay vì fallback, REPLAN initiative.
- Must not change: read actions của `twitter.py` và các channel read hiện có; `_pace_browser`/cooldown semantics trong `dispatch.py`.
- Touch point: `agent_reach/channels/_facebook_write.py` + `_facebook_vars.json` — pattern mẫu; `docs/fb-mutation-capture.md` — capture pipeline mẫu; registry contract của epic 11.
- Reuse point: capture pipeline phải generalize harness FB đã build (`scripts/capture_fb_mutation.py` + `tests/test_fb_capture.py`, fb story 1.1) — KHÔNG viết pipeline capture mới từ đầu.

## References

- parent — `initiative-multi-account-farm.md`, Requirements R1, R3, R4
- architecture — `docs/multi-account-farm-architecture.md` §4a (work items 1, 3, 4), §3 (replay constraint C4)
- code — `agent_reach/channels/_facebook_write.py`, `agent_reach/channels/twitter.py`, `agent_reach/runtime/dispatch.py`, `scripts/capture_fb_mutation.py` (harness để generalize)
- docs — `docs/fb-mutation-capture.md`
