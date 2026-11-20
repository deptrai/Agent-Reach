---
id: 1
type: epic
title: "Per-account substrate + contracts"
parent: initiative-multi-account-farm
covers: ["R15"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: medium
---

# Per-account substrate + contracts

## Description

Platform-baseline epic — thứ mà R5 đến R11 đều ngầm giả định nhưng doc gốc chưa liệt thành work item: một per-account registry là source of truth duy nhất (account ↔ proxy ↔ container ↔ session ↔ tuổi account ↔ rolling-24h ledger ↔ ban status), contract danh tính per-run ("run này là account_i với proxy_i"), và kill-switch contract viết đúng semantics một lần ở một chỗ: **zero egress ngoài proxy_i — fail-closed** (KHÔNG phải "verify exit IP = proxy", vì residential proxy rotate IP).

## Outcome

Caps, warm-up, kill-switch, fleet health và fallback — 5 epic sau — đọc-ghi cùng 1 format duy nhất; kill-switch có đúng 1 chỗ định nghĩa thay vì mỗi epic tự nghĩ một semantics.

## Done when

- Registry tồn tại (file/DB chọn lúc refine — đơn giản nhất thắng): CRUD account, lưu mapping proxy/container/session/age/ledger/status; có schema doc.
- Contract doc trong `docs/` định nghĩa: identity per-run, kill-switch 2 lớp (dispatch soft-check = best-effort pre-flight; netns hard-enforce = epic 4 bake), format ledger rolling-24h.
- Dispatch resolve được account_i → env_i/proxy_i từ registry (không cần container — env injection đủ cho phase này).
- Verify: tạo 2 account giả → 2 run song song nhận đúng proxy/env riêng, không đụng nhau.

## Boundaries

- Out: write lanes (epic 2), enforcement logic caps/warm-up (epic 3), container template/netns (epic 4 — chỉ định nghĩa contract env nó sẽ populate), metrics.
- Must not change: `_pace_browser`/cooldown sẵn có; bridge contract.
- Touch point: `agent_reach/runtime/dispatch.py` (hook đọc registry trước khi dispatch), `docs/` (contract doc), `agent_reach/runtime/session_reuse.py` (session field của registry).

## References

- parent — `initiative-multi-account-farm.md`, Requirement R15
- architecture — `docs/multi-account-farm-architecture.md` §3 (container-per-account), §5 (kill-switch, chain-ban)
- code — `agent_reach/runtime/dispatch.py`, `agent_reach/runtime/session_reuse.py`, `agent_reach/runtime/browser_watcher.py`
