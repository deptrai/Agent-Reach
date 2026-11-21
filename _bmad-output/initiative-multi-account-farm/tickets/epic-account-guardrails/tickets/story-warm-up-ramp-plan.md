---
title: 'Warm-up ramp theo tuổi account'
type: feature
ticket: '3'
created: '2026-11-22'
status: built
route: full
risk: low
context: []
warnings: []
deferred: []
---

## Intent

Caps chặt dần theo tuổi account: dispatch đọc `accounts.age` (onboard unix epoch)
→ tuổi theo ngày → scale cap 13.1 theo `RAMP_SCHEDULE` (d0-3: 25%, d4-7: 50%,
d8-14: 75%, d15+: 100%). Effective cap = `max(1, floor(cap × factor))` — cap 0
vẫn refuse-all, missing/unusable epoch → mature (không phạt account chưa migrate).

## Code Map

- `agent_reach/runtime/farm.py` — `account_age(name)` mới: SELECT `age`, trả int
  epoch hoặc None khi NULL/non-numeric (row migrated/hand-repaired). Unknown →
  NEED_SETUP như `account_proxy`.
- `agent_reach/runtime/dispatch.py` — `RAMP_SCHEDULE = ((3,.25),(7,.5),(14,.75))`
  chốt trong code; `REACH_RAMP="days:factor,..."` override (invalid → BAD_INPUT).
  `_enforce_write_quota` đọc `ledger_24h` + `account_age` trong cùng try
  (fail-closed → UPSTREAM_BROKEN), rồi compare `count >= effective` — ramp áp
  trước ledger compare. Message refusal có `cap × ramp (age Nd) = effective`
  khi cap bị scale; mature giữ message cũ.
- Skipped: `cap == 0` không đọc age, không parse REACH_RAMP — refuse-all nguyên
  trạng (ramp không hồi sinh cap 0).

## Notes

- **Known gap (không implement)**: engagement warm-up — like/follow ngẫu nhiên
  để nuôi account — vì write/engagement lanes chưa tồn tại trong runtime. Chỉ
  ramp cap tồn tại; khi lanes có thật, epic-fleet-orchestration hoặc lane riêng
  sẽ chủ động chạy warm-up traffic dưới cùng cap này.
- Tuổi tính `floor((now − age) / 86400)`; epoch 0/tương lai coi như missing →
  mature (age=now của `add_account` là day-0 hợp lệ → ramp strictest, đúng spec).
- `test_farm_quota.py::test_third_post...` giờ age account 30 ngày — vẫn test
  cap semantics 13.1 không bị ramp cắt.

## Verify

`age` = now−1d + `REACH_CAP_POST=10` → 2 posts ok, 3rd `QUOTA_EXCEEDED` với
"ramp 0.25"; `age` = now−30d → full 10. Sửa `age` trực tiếp trong registry
(UPDATE accounts) — đúng verify path của ticket.

## Auto Run Result

**Summary** — Warm-up ramp wired vào `_enforce_write_quota`: cap hiệu dụng =
`max(1, floor(cap × factor))` theo `RAMP_SCHEDULE`/`REACH_RAMP`, compare với
ledger 24h success. Missing epoch → mature; cap 0 → refuse-all; refusal
message giải thích `cap × ramp (age) = effective`.

**Files changed**
- `agent_reach/runtime/farm.py` — `account_age()` helper (epoch hoặc None).
- `agent_reach/runtime/dispatch.py` — `RAMP_SCHEDULE`, `_ramp_schedule`,
  `_ramp_factor`, `_account_age_days`; gate áp ramp trong `_enforce_write_quota`.
- `tests/test_farm_ramp.py` — 13 tests (boundaries param day 0/3/4/7/8/14/15,
  missing field, env override, invalid env, cap-0, floor-1).
- `tests/test_farm_quota.py` — `_mature()` helper cho test cap nguyên thủy.

**Verification performed** — `pytest tests -x -q` → 1023 passed (baseline 998
+ 13 ramp mới + 12 đổi/land kèm). Farm subset 122 green.

**Residual risks** — `REACH_RAMP` parse lỗi (BAD_INPUT) chỉ kiểm khi cap>0;
cap=0 short-circuit trước ramp parse — intentional, giữ nguyên semantics 13.1.
