---
title: 'Guardrails e2e + docs + sweep'
type: feature
ticket: '4'
created: '2026-11-22'
status: built
route: full
risk: low
context: []
warnings: []
deferred: []
---

## Intent

Đóng epic-account-guardrails: e2e trên X write lane (twitter post) xích qua
cả 3 guards — kill-switch → quota(+ramp) — rồi cập nhật contract §6 (quota,
kill-switch, ramp chuyển "Deferred" → "Implemented") và quét docs còn claim
lệch. Không behavior mới; refusal phải surface trong JSON envelope.

## Code Map

- `tests/test_farm_guardrails_e2e.py` — mới. `TwitterChannel` thật +
  `monkeypatch` `_post`; backend boom() fail nếu guard lọt. 5 tests:
  over-cap refuse, dead-proxy `PLATFORM_BLOCKED`, day-1 ramp (`cap 8 ×
  ramp 0.25 = 2`), chain ramp→kill-switch, CLI envelope (`unified.main` +
  capsys) cho `QUOTA_EXCEEDED` + `PLATFORM_BLOCKED`.
- `docs/farm-account-contract.md` — §2 layer-1 status → implemented; §6 table
  hàng `Layer-1 dispatch soft-check` và `Caps / warm-up` → Implemented
  (stories 13.1–13.3); thêm subsection "Implemented — dispatch guardrails"
  với env vars + ramp table + pointer tới playbook.
- `docs/farm-guardrails-playbook.md` — mới, operator runbook ngắn (caps,
  refusal envelope, proxy-block diagnosis, ramp tuning).
- `docs/multi-account-farm-architecture.md` — bullet "Chưa có" quota → đã có
  (link contract §6).

## Notes

- Guard order: `_enforce_proxy_kill_switch` trước `_enforce_write_quota`
  trong env window — dead proxy refuse kể cả khi quota cũng đã đầy (pinned bởi
  `test_kill_switch_runs_before_quota`; e2e chain verify lại).
- Live proxy = socket listen loopback ephemeral; dead = `127.0.0.1:1` —
  không mock ngoài network boundary trừ stub `_post`.
- Skip: không viết test cho DNS-unresolvable carve-out / verdict cache —
  unit tests 13.2 đã phủ; e2e giữ ở scenario chaining.

## Auto Run Result

**Summary** — E2e 5 tests trên X lane xanh (isolated HOME, registry thật):
cap-2 post-3 `QUOTA_EXCEEDED` ledger giữ 2 rows; dead proxy `PLATFORM_BLOCKED`
backend chưa chạy; age-1d `cap 8 x ramp 0.25 = 2`; chain ramp→kill-switch
PLATFORM_BLOCKED thắng quota; CLI `twitter post` in đúng JSON envelope cả 2
refusal codes. Docs: contract §2+§6 chuyển implemented + subsection mới;
architecture "Chưa có" quota sửa thành đã có; playbook mới.

**Files changed**
- `tests/test_farm_guardrails_e2e.py` (new)
- `docs/farm-account-contract.md`
- `docs/farm-guardrails-playbook.md` (new)
- `docs/multi-account-farm-architecture.md`

**Verification performed** — `pytest tests -x -q` → 1028 passed (baseline 1023
+ 5 e2e mới). e2e file chạy 0.54s.

**Residual risks** — e2e mock `_post` (không gọi browser thật) — đúng scope:
guards là dispatch-layer; live X write đã verify tay ở 12.x. DNS-uncheckable
proxy carve-out không test lại ở e2e level (đã có unit).
