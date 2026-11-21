---
title: 'Farm metrics tối giản per-account'
type: feature
ticket: '5'
created: '2026-11-22'
status: built
route: full
route_source: manual
risk: low
---

# Story 14.5 — Farm metrics tối giản per-account

Ticket: đọc `actions_log` của registry (epic-11.5) + health của fleet manager
→ output có cấu trúc (JSON file hoặc `agent-reach farm metrics`) gồm
actions/ngày per account per action_class, đếm RATE_LIMITED / PLATFORM_BLOCKED,
ban/suspension flags, health container. Không dashboard — chỉ sink đọc được
cho operator.

## Design

- **`agent_reach/runtime/farm_metrics.py`** (mới, ~190 dòng) — module read-only
  trên cùng `registry.db` của farm qua `farm.connect()` + `farm._with_busy_retry`
  (WAL semantics giống ledger). `metrics(account=None, window_hours=24)`:
  - Một query `GROUP BY account, action_class, outcome` trên window
    `ts >= now - hours*3600` → bucket per account:
    `actions: {success, attempts, by_class{cls:{success,attempts}}}`
    (actions/ngày = success count trong window, giống `ledger_24h`),
    `errors: {rate_limited, platform_blocked, quota_exceeded,
    persisted_query_dead, other}` + `by_outcome` giữ raw count mọi outcome
    (không mất NEED_LOGIN/TIMEOUT… trong `other`).
  - `ban_status` + `age_days` từ row `accounts` (age NULL/không số → None,
    cùng semantics `account_age`).
  - `fleet` section (chỉ khi `fleet_containers` có rows): reuse
    `fleet.list_fleet()` → per container `{name, account, session_state,
    ban_status, last_check_ts, last_check_age_s, pending_login, healthy,
    last_error}` + `total`/`healthy`. `--account` filter cả fleet theo
    `account` column (bridge epic-11 của 14.4 đã mirror).
  - `report()` = metrics() + sink: `--file` thắng `REACH_METRICS_FILE`;
    `append_jsonl` ghi 1 dòng JSONL/run (`O_APPEND|O_CREAT`, 0600) — cron
    drain. `sink_file` thêm vào payload trả về sau khi ghi (dòng trong file
    là payload metrics thuần).
- **CLI** `_cmd_farm` + op `metrics`: `agent-reach farm metrics
  [--account NAME] [--hours N] [--file PATH]` — envelope chuẩn
  `{ok, action:"farm metrics", result}`; `--hours` float → BAD_INPUT khi
  ≤0 hoặc >1 năm.

## Verify

- `tests/test_farm_metrics.py` — 8 tests mới: đếm 2 account mixed outcomes
  (đủ 4 error buckets + other=TIMEOUT), window filtering (24h drop, 48h
  nhận), single-account scope + NEED_SETUP/BAD_INPUT, ban_status+age_days,
  fleet section (present khi có rows / absent khi không, scope theo
  --account), CLI envelope shape + usage errors, `--file` JSONL append 2
  dòng + mode 0600 + `REACH_METRICS_FILE` fallback.
- Full suite: **1075 passed** (`uv run --extra dev python -m pytest tests -x -q`).
- Live: `uv run agent-reach farm metrics --account a1 --hours 12 --file /tmp/m.jsonl`.

## Deviations

- "uptime container" trong ticket → fleet table không có cột start-time;
  surface `session_state` + `last_check_age_s` + `healthy` thay thế (đủ cho
  operator thấy container nào up/down bao lâu qua poll cadence). Thêm cột
  started_at là việc của 14.4 schema — out of scope.
- Bỏ `quota:{cap_class: effective_cap}` (optional trong spec) — cần import
  private `_cap_for`/`_ramp_factor` của dispatch, coupling không đáng; cap
  hiệu lực đã đọc được qua `farm account ledger` + env override.
- Thêm `by_outcome` ngoài 4 bucket ticket yêu cầu — `other` count trần giấu
  spike NEED_LOGIN/TIMEOUT khỏi operator; cost là 1 dict đã tính sẵn.

## Files

- `agent_reach/runtime/farm_metrics.py` — mới.
- `agent_reach/runtime/cli.py` — op `metrics` trong `_cmd_farm` (+3 argparse
  flags) + HELP line.
- `tests/test_farm_metrics.py` — mới, 8 tests.

## Risks / pending

- `farm._with_busy_retry` / `farm._validate_name` là private helpers dùng
  chéo trong cùng package — đổi tên ở farm.py phải sửa đây (tests bắt được).
- JSONL sink không rotate — cron ghi 1 dòng/run, operator tự rotate.
- `--hours` float chấp nhận 0.5h; cap trên 1 năm chỉ để chặn typo vô nghĩa.

## Auto Run Result

- **Summary**: module `farm_metrics.py` + CLI `agent-reach farm metrics
  [--account|--hours|--file]` — GROUP BY actions_log theo rolling window →
  per-account actions/errors (4 bucket + other + by_outcome), ban_status,
  age_days; fleet section reuse `fleet.list_fleet()` khi có rows; JSONL sink
  0600 qua `--file`/`REACH_METRICS_FILE` cho cron.
- **Files**: `farm_metrics.py` (mới), `cli.py` (+~20), `test_farm_metrics.py`
  (mới, 8 tests), plan file này.
- **Verification**: 1075/1075 pass; ticket acceptance (≥2 account mixed
  outcomes → đủ fields actions/day, rate_limited, platform_blocked, ban
  flags, per account) cover bởi test 1 + test 6.
- **Residual risks**: uptime container chỉ có dạng session_state +
  last_check_age_s (fleet schema thiếu start-ts); quota effective cap chưa
  surface (optional, cần dispatch privates).
