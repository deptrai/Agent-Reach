---
id: 3
type: epic
title: "Container fleet orchestration"
parent: initiative-multi-account-farm
covers: ["R8", "R9", "R10", "R11"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: high
---

# Container fleet orchestration

## Description

Hạ tầng vận hành thật của farm: container template (Docker/Podman) chạy 1 account với HOME riêng, `~/.agent-reach` volume riêng, `REACH_BROWSER_PROXY` riêng, network namespace riêng để bridge `127.0.0.1:19825` không đụng nhau; phía trên là fleet manager poll `agent-reach check` JSON để biết browser/proxy/session/cooldown per container, automation mở lại login window khi `NEED_LOGIN`, và metrics ban-rate tối giản.

## Outcome

Chủ farm nhân bản 1 account thành N container bằng template, nhìn 1 dashboard biết container nào đỏ và vì sao, account hết session được mở login window đúng chỗ thay vì mò tay từng cái.

## Done when

- Container template chạy được `agent-reach check` + `x post` từ trong container riêng với proxy riêng, resource cap ~1.5GB RAM, proxy kill-switch bake vào network layer (proxy chết → không có route ra ngoài).
- ≥2 container chạy song song trên 1 host không đụng bridge/proxy/session của nhau; fleet manager hiển thị health per container.
- `NEED_LOGIN` trên 1 container → job mở login window trong đúng container đó.
- Template pin OpenCLI/Agent-Reach version tại build time (C7) — upgrade = rebuild có kiểm soát, không auto-update trong container đang chạy.
- Metrics per-account (action/ngày, RATE_LIMITED, PLATFORM_BLOCKED, ban) ghi ra nơi đọc được, dashboard tối giản hoặc output có cấu trúc.

## Boundaries

- Out: write lanes (epic 1), logic caps/kill-switch check trong dispatch (epic 2 — epic này bake contract đó vào infra), XActions fallback (epic 4).
- Procurement proxy pool và nâng RAM server là việc con người — hitl entry khi plan stories.
- Must not change: bridge contract `127.0.0.1:19825` (tách bằng netns, không đổi port/config trong code).
- Touch point: `agent_reach/runtime/onboarding.py` (login flow designed-for-agent), `browser_watcher.py` (proxy launch, CloakBrowser), `bridge.py` (singleton → netns), `runtime/tools.py` (version pin C7).

## References

- parent — `initiative-multi-account-farm.md`, Requirements R8–R11
- architecture — `docs/multi-account-farm-architecture.md` §3 (container-per-account), §4c (work items 8–11), C1/C2/C6
- code — `agent_reach/runtime/onboarding.py`, `agent_reach/runtime/bridge.py`, `agent_reach/runtime/browser_watcher.py`, `agent_reach/runtime/session_reuse.py`
