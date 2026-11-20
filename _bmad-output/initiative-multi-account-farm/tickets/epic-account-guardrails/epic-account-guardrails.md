---
id: 2
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

Lớp chính sách per-account gắn vào dispatch, chạy trước mọi write action: quota theo ngày theo action-class (model `action-caps` rolling-24h của XActions), lịch warm-up ramp-up cho account mới, và proxy kill-switch verify exit IP trước khi action — fail-closed `PLATFORM_BLOCKED`, không fallback direct. Đây là policy layer cross-platform, không phải một lane.

## Outcome

Một account mới vào farm không thể tự bắn mình: vượt cap bị refuse trước khi gọi platform, proxy chết thì action dừng thay vì leak IP nhà, hành vi ramp-up tự động thay vì post burst.

## Done when

- Write caps per-account per-action-class per-day enforce được trong dispatch, env-configurable, ledger rolling-24h persist được per account.
- Kill-switch check verify exit IP = proxy của account trước mọi action; proxy chết → `PLATFORM_BLOCKED`, không packet nào đi direct.
- Warm-up schedule áp dụng được cho account mới (hành vi bị giới hạn theo ngày tuổi của account).
- Verify trên ≥1 account thật: cố tình vượt cap → refuse; kill proxy → fail-closed.

## Boundaries

- Out: write lanes tự thân (epic 1), container networking thật (epic 3 — epic này chỉ định nghĩa kill-switch *contract* mà container bake vào), metrics/dashboard (epic 3).
- Hitl: cần ≥1 proxy thật trong dev env để verify fail-closed — 1 cái là đủ, không chờ cả proxy pool của epic 3.
- Must not change: `_pace_browser`, `RATE_LIMIT_SECONDS`, `BACKEND_PAUSE_SECONDS` semantics sẵn có — caps per-account bổ sung lớp trên, không thay thế.
- Touch point: `agent_reach/runtime/dispatch.py` — chỗ enforce; XActions `src/mcp/action-caps.js` — model tham chiếu.

## References

- parent — `initiative-multi-account-farm.md`, Requirements R5–R7
- architecture — `docs/multi-account-farm-architecture.md` §4b (work items 5–7), §5 (risk: chain-ban qua IP/behavior)
- code — `agent_reach/runtime/dispatch.py`; `../XActions/src/mcp/action-caps.js` (ledger model)
