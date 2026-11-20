# Elicitation — slice initiative-multi-account-farm

## Round 1 — 2026-11-21

**Target:** `_bmad-output/initiative-multi-account-farm/` (initiative + tickets.toml + 6 epic files), trên grounding `docs/multi-account-farm-architecture.md`. Mục tiêu: cold-read trước khi refine stories.

**Pushes (3 hướng khác nhau, song song, fresh context):**
- `round-1-push-1-assumption-audit.md` — 13 giả định được rate, 3 yếu nhất stress-tested (verify song song của epic 1, identity+ledger kép ở fallback, registry concurrency).
- `round-1-push-2-cascading-failure.md` — trigger X rotate doc-id → trace đường lan: fallback thừa hưởng cùng failure, drift monitor là detector duy nhất, caps bị đốt bởi outage.
- `round-1-push-3-consistency-check.md` — 8 cặp mâu thuẫn, nặng nhất: epic-farm-resilience còn numbering slice 4-epic cũ, toml thiếu edge epic4→epic2.

### Proposed revision

| # | Location | Đổi | Push |
|---|----------|-----|------|
| 1 | epic-farm-resilience | `id: 4`→`id: 6`; renumber toàn bộ cross-ref sang slug (epic-account-guardrails / epic-fleet-orchestration / epic-write-lanes) | P1, P2, P3 |
| 2 | tickets.toml | epic 4 `after` thêm `{epic=2}` (cần X lane cho verify `x post`); epic 3 `after` thêm `{epic=2}` (cần live write action làm verify target) | P3, P1 |
| 3 | epic-account-substrate Done-when | "2 run song song" → verify tuần tự trên 2 HOME giả lập + contract phải định nghĩa isolation mechanism (HOME + bridge bind per-account) để song song thật ở epic 4 | P1, P3 |
| 4 | epic-account-substrate contract | thêm: concurrency/locking model cho ledger đa-writer (vd SQLite WAL); ledger chỉ đếm action thành công, attempt tách riêng; fallback writes cũng ghi ledger | P1, P2 |
| 5 | epic-write-lanes Done-when | clarify scope externalize = toàn bộ persisted queries X đang dùng (kể cả reads) hoặc descope reads kèm lý do; thêm probe doc-id từ ≥2 account trước khi chốt global; verify-env = host + env_i/proxy_i (container-proof ở epic 4) | P2, P1, P3 |
| 6 | epic-farm-resilience Done-when | thêm error class `PERSISTED_QUERY_DEAD` + rule route deterministic sang fallback; spec drift monitor (cadence, alert sink, probe cả XActions endpoint store); fallback story gồm bước sync config XActions | P2 |
| 7 | epic-fleet-orchestration Done-when | thêm cap: runbook nhân bản tối đa ≤20 account tới khi epic 6 playbook + drift monitor live | P2, P3 |
| 8 | epic-farm-resilience scope | thêm "identity bridge Reach↔XActions": mapping account, provision session/cookie, ban-status write-back về registry | P1 |
| 9 | epic-write-lanes + epic-farm-resilience Boundaries | delineate drift: epic 2 = on-demand config check (doctor), epic 6 = proactive probe + alert | P3 |
| 10 | epic-platform-lanes | cite §4d→§4a item 2 (+§5); ghi rõ ai cung cấp test account/session per platform; story đầu mỗi lane = login-state verification | P3, P1 |
| 11 | epic-account-guardrails | field `age` populate lúc onboarding (epic 1 CRUD); Notes: warm-up chỉ cover frequency — engagement warm-up (like/follow) là known gap | P1 |
| 12 | cả 6 epic Boundaries | thêm dòng "Dependencies authoritative: tickets.toml" | P3 |

### Set aside

- A5/A6/A12 (check JSON đủ tín hiệu, NEED_LOGIN automation, 1.5GB budget): là giả định verify-lúc-refine, không phải edit tài liệu — đã có sẵn trong risk/Open questions.
- A4 (X capture feasibility): đã có spike + escape REPLAN trong epic 2.
- "Đưa persisted-query liveness vào `agent-reach check`" (P2): đề xuất mở rộng phạm vi code, ghi nhận là câu hỏi mở ở epic 4 thay vì sửa Done-when.

### Decisions cho user

- **D1** — Scale gate: đề xuất cap runbook ≤20 ở epic 4 (change 7) thay vì tách dep epic 6 — đơn giản hơn, không cần re-slice.
- **D2** — Externalize scope: đề xuất gồm cả read queryIds (change 5) vì cùng rotation giết read surface; alternative = descope reads kèm lý do.

**Outcome:** user chọn **apply all** 2026-11-21 — cả 12 changes đã apply vào 7 file (tickets.toml + 6 epics + initiative Notes), graph re-check clean (`tickets.py status`: 0 unpinned/undeclared/conflict). Committed.
