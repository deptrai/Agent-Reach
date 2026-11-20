---
id: 13
type: epic
title: "Per-account guardrails"
parent: initiative-multi-account-farm
covers: ["R5", "R6", "R7"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: medium
---

# Per-account guardrails

## Description

Lớp chính sách per-account gắn vào dispatch, chạy trước **mọi action** (không chỉ write — read qua proxy chết cũng leak IP nhà y hệt): quota theo ngày theo action-class đọc ledger rolling-24h từ registry, lịch warm-up ramp theo tuổi account trong registry, và kill-switch soft enforce theo contract của epic 11 — **zero egress ngoài proxy_i**, dispatch là best-effort pre-flight, netns hard-enforce là của epic 14.

## Outcome

Một account mới vào farm không thể tự bắn mình: vượt cap bị refuse trước khi gọi platform, proxy chết thì action dừng thay vì leak IP nhà, tần suất action ramp dần theo tuổi thay vì burst.

## Done when

- Write caps per-account per-action-class per-day enforce được trong dispatch, env-configurable, ledger rolling-24h đọc-ghi qua registry của epic 11.
- Kill-switch: dispatch refuse + `PLATFORM_BLOCKED` khi proxy của account fail check, **không** hành nào đi direct; semantics theo đúng contract doc epic 11 (zero egress — không so IP tuyệt đối vì residential rotate).
- Warm-up: caps chặt dần theo tuổi account (ngày tuổi từ field `age` của registry — epic-account-substrate populate lúc onboard; chỉ điều chỉnh tần suất action — **engagement warm-up kiểu like/follow là known gap vì lane chưa tồn tại**, ghi nhận trong Notes).
- Verify trên X write lane của epic-write-lanes: cố tình vượt cap → refuse; kill proxy → fail-closed.
- Refusal phải hiện lên operator (log/status rõ ràng — không âm thầm nuốt).

## Boundaries

- Out: write lanes tự thân (epic-write-lanes), netns/container hard-enforce (epic-fleet-orchestration — epic này chỉ soft enforce ở dispatch theo contract), metrics/dashboard (epic-fleet-orchestration).
- Dependencies authoritative: `tickets/tickets.toml`.
- Must not change: `_pace_browser`, `RATE_LIMIT_SECONDS`, `BACKEND_PAUSE_SECONDS` semantics sẵn có — caps per-account bổ sung lớp trên, không thay thế.
- Hitl: cần ≥1 proxy thật trong dev env để verify fail-closed — 1 cái là đủ, không chờ cả proxy pool của epic 14.
- Touch point: `agent_reach/runtime/dispatch.py` — chỗ enforce; registry + kill-switch contract của epic 11; XActions `src/mcp/action-caps.js` — model ledger tham chiếu.

## References

- parent — `initiative-multi-account-farm.md`, Requirements R5–R7
- architecture — `docs/multi-account-farm-architecture.md` §4b (work items 5–7), §5 (risk: chain-ban qua IP/behavior)
- code — `agent_reach/runtime/dispatch.py`; `../XActions/src/mcp/action-caps.js` (ledger model)
