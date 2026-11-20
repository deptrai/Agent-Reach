---
id: 4
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

- Fallback path hoạt động: 1 mutation replay gãy (hoặc lane chưa có) → action route về `xactions-mcp` HTTP lane với kết quả verify được; write qua fallback vẫn đi qua enforcement caps/kill-switch của epic 2 (không bypass).
- Fallback egress đi qua proxy của account (theo C3) — wiring phía lane nằm ở epic này, dựa trên env per-account mà container template epic 3 cung cấp; không accept degrade direct-leak-IP.
- Doc-id drift monitor chạy host-level (doc-id là global theo platform, không per-account → 1 cron là đủ); kill 1 doc-id giả lập → alert phát ra.
- Incident playbook tồn tại trong docs, đã walk-through ≥1 scenario — land đủ sớm trong program (không đợi epic cuối hàng xong mới viết, vì farm vượt 20 account ngay sau khi epic 4 scale).

## Boundaries

- Out: sửa TLS fingerprint của HTTP lane (C5 — fallback là degrade, không phải lane chính); metrics/fleet dashboard (epic 3); version pin OpenCLI — nằm ở container template epic 3, playbook chỉ cover quy trình canary/update.
- Must not change: hành vi mặc định của write lanes — fallback chỉ kích hoạt khi replay lỗi, không thay lane chính; enforcement path của epic 2 — fallback đi qua nó, không đi vòng.
- Touch point: repo XActions (`../XActions`, `main-clean`) — `xactions-mcp` HTTP lane + proxy wiring phía lane; config doc-ids mà epic 1 externalize (drift monitor đọc nó).

## References

- parent — `initiative-multi-account-farm.md`, Requirements R12–R14
- architecture — `docs/multi-account-farm-architecture.md` §4d (work items 12–14), §5 (risk: platform đổi doc-id, OpenCLI update gãy)
- code — `../XActions` (xactions-mcp HTTP lane, `action-caps.js`)
