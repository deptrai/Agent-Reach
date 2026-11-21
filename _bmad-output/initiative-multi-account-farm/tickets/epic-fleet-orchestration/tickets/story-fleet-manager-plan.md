---
title: 'Fleet manager — health poll + registry populate + NEED_LOGIN reopen'
type: feature
ticket: '4'
created: '2026-11-22'
status: built
route: full
route_source: manual
risk: medium
---

# Story 14.4 — Fleet manager

Ticket: poll `agent-reach check` JSON trong mỗi container theo cadence →
populate runtime fields registry epic-11 (container ↔ account ↔ proxy ↔
session_state ↔ ban_status); NEED_LOGIN → mở login window trong đúng container
đó, ghi trạng thái chờ operator; health view 1 chỗ.

## Design

- **`agent_reach/runtime/fleet.py`** (mới, ~370 dòng) — module độc lập mở cùng
  `registry.db` của farm (WAL, private) qua `farm.connect()` + tự apply table
  riêng `fleet_containers`; `farm.py` không đổi. Columns: `name` (PK),
  `container_id`, `account`, `proxy`, `image`, `session_state`, `ban_status`,
  `last_check_ts`, `last_error`, `pending_login`, `login_channel`, `updated_at`.
- **Discovery**: `docker ps -a --format json`, filter container `farm-*` hoặc
  label `com.agent-reach.farm=1` (compose giờ set cả hai +
  `com.agent-reach.account`). `account` lấy từ label, fallback name-prefix;
  `proxy`/`ban_status` mirror từ `farm.list_accounts()` (bridge epic-11).
- **Poll** (`poll_once`, cadence `--interval`/`REACH_FLEET_POLL` default 300s):
  `docker exec -u farm <name> agent-reach check` với budget
  `REACH_FLEET_TIMEOUT` (default 60s) → parse envelope:
  - `ok` → `ok` (clear `pending_login`)
  - result có `error.code == NEED_LOGIN` → `NEED_LOGIN`, ghi `login_channel`
  - fail khác → `degraded` (codes gom trong `last_error`)
  - exec fail/timeout/không-JSON → `unreachable`; container không `running` →
    `down`; biến mất khỏi `docker ps` → `down` ("not present in docker ps").
- **NEED_LOGIN reopen**: transition vào NEED_LOGIN + `pending_login=0` → fire
  **một lần** `docker exec -u farm -d <name> agent-reach login <channel>`
  (detached — không block poll; `-u farm` bắt buộc vì 14.3 giữ PID1 root cho
  kill-switch, exec root sẽ vào /root sai HOME). Set `pending_login=1` +
  `last_error` "...pending operator". Polls sau không refire; `ok` clear flag.
  Spawn fail → không set pending, lần sau retry được (prev vẫn NEED_LOGIN nên
  vẫn không spam? — transition gate `prev != NEED_LOGIN` chặn refire tự nhiên).
- **`fleet relogin <name> [--channel ch]`**: manual reopen — dùng
  `login_channel` đã ghi, hoặc bắt `--channel`; NEED_SETUP nếu container
  unknown, BAD_INPUT nếu thiếu channel.

## CLI surface (`_cmd_fleet` trong `runtime/cli.py`)

```
agent-reach fleet list                                  health view 1 chỗ
agent-reach fleet poll [--once] [--interval SECONDS]    cron --once hoặc daemon loop
agent-reach fleet relogin <container> [--channel <ch>]  reopen login window thủ công
```

`fleet list` per container: `name, container_id, account, proxy, image,
session_state, ban_status, last_check_ts, last_check_at, last_check_age_s,
last_error, pending_login, login_channel, healthy` — `healthy` chỉ `true` khi
`session_state == "ok"`.

## Verify

- `tests/test_fleet.py` — 11 tests mới (discover filter, poll populate +
  ban_status/proxy bridge, NEED_LOGIN fire-once + không refire, pending clear
  on recovery, unreachable, down-không-exec, degraded, list envelope, poll
  --once CLI, relogin manual recorded-channel, unknown container → NEED_SETUP).
- Full suite: **1067 passed** (`uv run --extra dev python -m pytest tests -x -q`).
- **Docker live smoke trên host này — ĐÃ CHẠY:**
  - `docker ps -a --format json` thật → discover nhận `farm-ks-iter`
    (container của 14.3, name prefix — label fallback hoạt động, không cần
    compose label).
  - `agent-reach fleet poll --once` live → `docker exec -u farm farm-ks-iter
    agent-reach check` chạy thật trong container, parse JSON envelope →
    `session_state=degraded` với `last_error` chi tiết NEED_SETUP/
    UPSTREAM_BROKEN (kill-switch chặn egress → đúng kỳ vọng), row upsert đủ
    fields, `last_check_age_s` hợp lệ.
  - `agent-reach fleet list` live → envelope đúng shape.
  - `agent-reach fleet relogin farm-ks-iter --channel twitter` live →
    `docker exec -u farm -d ... login twitter` rc=0, `pending_login=true` +
    note operator. Container vẫn chạy bình thường.
  - **2-container live verify (ticket acceptance) = pending operator**: cần 2
    account env + 2 compose instance; heavy vì amd64 emulation. Đã chứng minh
    đủ đường code trên 1 container thật + stub cho state machine.

## Deviations

- `docker ps -a` thay `docker ps` trong discovery — để `fleet list` thấy cả
  container đã stop (`down`), đúng tinh thần health view; ticket ghi `docker
  ps --format json`, dùng superset `-a`.
- `docker exec -u farm` (không có trong mô tả ticket) — bắt buộc sau 14.3 đổi
  container sang `user: root` + `cap_add: NET_ADMIN` cho kill-switch; exec
  không `-u` chạy trong /root → sai HOME, check vô nghĩa.
- Thêm cột `login_channel` ngoài list cột ticket — cần để `fleet relogin`
  không bắt operator gõ lại channel và để auto-reopen đúng channel chết.
- `session_state` thêm giá trị `degraded` cho fail không-phải-NEED_LOGIN
  (PLATFORM_BLOCKED/UPSTREAM_BROKEN/NEED_SETUP) — ticket chỉ nêu ok/NEED_LOGIN/
  down; cần phân biệt "check chạy được nhưng đỏ vì lý do khác" vs "không tới
  được container".

## Files

- `agent_reach/runtime/fleet.py` — mới.
- `agent_reach/runtime/cli.py` — `_cmd_fleet` + routing `fleet` + HELP line.
- `farm/container/docker-compose.yml` — `labels: com.agent-reach.farm=1,
  com.agent-reach.account=$REACH_ACCOUNT` (không đụng phần 14.3).
- `tests/test_fleet.py` — mới, 11 tests.

## Risks / pending

- 2-container live verify pending operator (xem trên).
- Relogin window mở trong container headless — operator cần đường vào
  (VNC/attach theo runbook 14.x); fleet chỉ ghi `pending_login` + note, không
  tự hoàn tất login.
- `pending_login` persist trong DB qua restart — container chết/có login khác
  hoàn tất thì poll `ok` clear tự nhiên.
- Compose label chỉ có trên instance compose mới; container chạy `docker run`
  tay vẫn được nhận qua prefix `farm-`.

## Auto Run Result

- **Summary**: module `fleet.py` + CLI `agent-reach fleet list|poll [--once]
  [--interval]|relogin` — poll `agent-reach check` trong từng farm container
  (`docker exec -u farm`), upsert bảng `fleet_containers` trong cùng
  registry.db (farm.py untouched), NEED_LOGIN → detached `docker exec -u farm
  -d ... login <channel>` một lần/transition + pending_login cho operator.
- **Files**: `fleet.py` (mới), `cli.py` (+~50: `_cmd_fleet`/routing/HELP),
  `docker-compose.yml` (+labels), `test_fleet.py` (mới, 11 tests).
- **Verification**: 1067/1067 pass; live smoke 1 container thật
  (`farm-ks-iter`): poll/exec/list/relogin chạy đúng, `degraded` đúng vì
  kill-switch; 2-container live = pending operator.
- **Residual risks**: relogin cần operator access vào browser trong container;
  container down mất cửa sổ login đang pending (flag reset khi down — refire
  trên transition kế tiếp).
