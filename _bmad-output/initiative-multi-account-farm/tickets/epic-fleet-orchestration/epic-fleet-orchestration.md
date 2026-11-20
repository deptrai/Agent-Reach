---
id: 14
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

Hạ tầng vận hành thật của farm: container template (Docker/Podman) chạy 1 account với HOME riêng, `~/.agent-reach` volume riêng, proxy riêng, network namespace riêng để bridge `127.0.0.1:19825` không đụng nhau và **netns hard-enforce kill-switch contract** (zero egress ngoài proxy_i — contract ở epic 11); phía trên là fleet manager poll `agent-reach check` JSON per container populate registry, automation mở lại login window khi `NEED_LOGIN`, và metrics ban-rate tối giản.

## Outcome

Chủ farm nhân bản 1 account thành N container bằng template, nhìn 1 chỗ biết container nào đỏ và vì sao, account hết session được mở login window đúng chỗ — và farm chứng minh được mình scale thật, không dừng ở demo 2 cái.

## Done when

- Container template chạy được `agent-reach check` + `x post` từ trong container riêng với proxy riêng, resource cap ~1.5GB RAM; netns bake kill-switch contract của epic 11 (proxy chết → không có route ra ngoài — hard guarantee, không chờ dispatch check).
- **≥4 container chạy song song trên 1 host** không đụng bridge/proxy/session của nhau + runbook nhân bản 2→N trong `docs/` — gate scale thật của initiative đứng ở đây; **runbook cap ≤20 account tới khi epic-farm-resilience (playbook + drift monitor) live** — không cho farm scale qua mốc đó với zero fallback/zero monitor.
- Fleet manager hiển thị health per container; runtime fields của registry (container↔account↔proxy↔session↔ban status) được populate.
- `NEED_LOGIN` trên 1 container → job mở login window trong đúng container đó.
- Metrics per-account (action/ngày, RATE_LIMITED, PLATFORM_BLOCKED, ban) ghi ra nơi đọc được, dashboard tối giản hoặc output có cấu trúc.
- Template pin OpenCLI/Agent-Reach version tại build time (C7) — upgrade = rebuild có kiểm soát, không auto-update trong container đang chạy.

## Boundaries

- Out: write lanes (epic-write-lanes), logic caps/kill-switch check trong dispatch (epic-account-guardrails — epic này bake contract đó vào netns, không viết lại policy), registry schema (epic-account-substrate — epic này chỉ populate), XActions fallback (epic-farm-resilience — chỉ cung cấp env per-account).
- Dependencies authoritative: `tickets/tickets.toml`.
- Procurement proxy pool và nâng RAM server là việc con người — hitl entry khi plan stories.
- Must not change: bridge contract `127.0.0.1:19825` (tách bằng netns, không đổi port/config trong code); registry schema của epic 11.
- Touch point: `agent_reach/runtime/onboarding.py` (login flow designed-for-agent), `browser_watcher.py` (proxy launch, CloakBrowser), `bridge.py` (singleton → netns), `runtime/tools.py` (version pin C7), registry của epic 11.

## References

- parent — `initiative-multi-account-farm.md`, Requirements R8–R11
- architecture — `docs/multi-account-farm-architecture.md` §3 (container-per-account), §4c (work items 8–11), C1/C2/C6
- code — `agent_reach/runtime/onboarding.py`, `agent_reach/runtime/bridge.py`, `agent_reach/runtime/browser_watcher.py`, `agent_reach/runtime/session_reuse.py`
