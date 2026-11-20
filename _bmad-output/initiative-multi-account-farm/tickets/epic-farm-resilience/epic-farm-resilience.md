---
id: 16
type: epic
title: "Farm resilience"
parent: initiative-multi-account-farm
covers: ["R12", "R13", "R14"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: medium
---

# Farm resilience

## Description

Lớp chống chết hàng loạt: khi mutation replay của 1 platform gãy, account tự degrade về XActions HTTP lane (đã verify live: post/thread/reply/quote/schedule + media); incident playbook cho 3 scenario (account suspend, proxy pool hỏng, OpenCLI update gãy); cron drift monitor probe các persisted doc-id đang dùng và alert trước khi platform đổi query-id làm cả farm chết cùng lúc.

## Outcome

Farm degrade graceful thay vì chết đứng: 1 mutation gãy → account vẫn post được qua fallback; doc-id chết → alert trước khi phát hiện qua hàng loạt action fail; sự cố vận hành → có playbook chạy theo thay vì xử lý mò.

## Done when

- Fallback path hoạt động: mutation replay gãy với error class `PERSISTED_QUERY_DEAD` / upstream-schema (định nghĩa trong contract epic-account-substrate — **không** được nuốt vào semantics `RATE_LIMITED`/`PLATFORM_BLOCKED` no-retry của dispatch) → route deterministic về `xactions-mcp` HTTP lane với kết quả verify được; write qua fallback vẫn đi qua enforcement caps/kill-switch của epic-account-guardrails (không bypass).
- Fallback egress đi qua proxy của account (theo C3) — wiring phía lane nằm ở epic này, dựa trên env per-account mà container template epic-fleet-orchestration cung cấp; không accept degrade direct-leak-IP.
- Identity bridge Reach↔XActions định nghĩa rõ: mapping account_i ↔ entry trong XActions accountPool, cách provision session/cookie sang vault của XActions, và ban-status phát hiện phía XActions ghi ngược về registry — fallback không tạo hệ account song song mờ.
- Fallback story bao gồm bước đồng bộ config XActions (`endpoint.url`) khi doc-id rotate — fallback không che được failure persisted-query nếu store của nó cũng stale cùng lúc (single source hoặc sync path giữa 2 store).
- Doc-id drift monitor chạy host-level (doc-id là global theo platform → 1 cron là đủ) với cadence + alert sink spec rõ trong contract; probe cả 2 config store (vars của Agent-Reach và `endpoint.url` của XActions); kill 1 doc-id giả lập → alert phát ra.
- Incident playbook tồn tại trong docs, đã walk-through ≥1 scenario — land đủ sớm trong program (không đợi epic cuối hàng xong mới viết, vì farm vượt 20 account ngay sau khi epic-fleet-orchestration scale).

## Boundaries

- Out: sửa TLS fingerprint của HTTP lane (C5 — fallback là degrade, không phải lane chính); metrics/fleet dashboard (epic-fleet-orchestration); version pin OpenCLI — nằm ở container template epic-fleet-orchestration, playbook chỉ cover quy trình canary/update.
- Drift delineation: epic này = proactive probe + alert; on-demand config check (`agent-reach doctor`) thuộc epic-write-lanes.
- Dependencies authoritative: `tickets/tickets.toml`.
- Must not change: hành vi mặc định của write lanes — fallback chỉ kích hoạt khi replay lỗi, không thay lane chính; enforcement path của epic-account-guardrails — fallback đi qua nó, không đi vòng.
- Touch point: repo XActions (`../XActions`, `main-clean`) — `xactions-mcp` HTTP lane + proxy wiring phía lane + accountPool identity bridge; config doc-ids mà epic-write-lanes externalize (drift monitor đọc nó).

## References

- parent — `initiative-multi-account-farm.md`, Requirements R12–R14
- architecture — `docs/multi-account-farm-architecture.md` §4d (work items 12–14), §5 (risk: platform đổi doc-id, OpenCLI update gãy)
- code — `../XActions` (xactions-mcp HTTP lane, `action-caps.js`, `accountPool.js`)
