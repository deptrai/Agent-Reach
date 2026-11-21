---
type: feature
ticket: '2'
status: built
route: full
risk: high
baseline_revision: c2733d5
---

# Story 14.2 — Container template: 1 account : 1 container

## Kết quả

Template Docker hoàn chỉnh tại `farm/container/` + backend `shardbrowser`
mới song song `cloakbrowser`, chọn qua env `REACH_BROWSER_BACKEND=shard`
(bake sẵn trong image). CloakBrowser host-local không đổi.

## Files

### Mới
- `agent_reach/backends/shard.py` — backend ShardBrowser: `setup()` parity
  `cloak.setup()` (resolve/install + report), `launch()` gọi đúng call đã
  verify ở spike (`ShardX().launch(profile, proxy=, cdp=True, headless=,
  user_data_dir=, extra_args=)`), `_wait_cdp` poll `/json/version` qua
  loopback không proxy, `close_session()` chịu mọi close API của SDK,
  `python -m agent_reach.backends.shard install|version|path` cho build.
- `farm/container/Dockerfile` — pin đủ C7 (bảng dưới).
- `farm/container/Dockerfile.dockerignore` — ignore riêng cho build context
  repo root.
- `farm/container/docker-compose.yml` — 1 service `account`, instantiate per
  account bằng `docker compose -p farm-<account>` → container + named volume
  riêng; `mem_limit: 1536m`.
- `farm/container/entrypoint.sh` — seed extension pin sẵn vào HOME volume
  lần boot đầu, rồi `agent-reach check` (JSON ra stdout, exit code = health;
  `FARM_BOOT_CHECK=0` bypass lúc onboarding), cuối `exec "$@"`.
- `farm/container/verify.sh` — acceptance steps trên Docker host.
- `farm/container/.env.example`, `farm/container/README.md`.
- `tests/test_container_template.py` — 11 tests.

### Sửa
- `agent_reach/runtime/browser.py` — `ensure_chrome()` nhánh
  `REACH_BROWSER_BACKEND=shard` → `backends.shard.setup()` (backend
  `shardbrowser`); `_launch()` truyền `--shard` cho watcher khi backend là
  shardbrowser; **`REACH_BROWSER_PROXY` env giờ thắng Config** (proxy per
  container — farm.py set env cho child, trước đây bị Config ghi đè rỗng).
- `agent_reach/runtime/browser_watcher.py` — flag `--shard`, `_launch_shard()`
  đọc `REACH_BROWSER_PROXY`/`REACH_BROWSER_PROFILE` từ env, session thay ctx
  trong loop (gone-check qua CDP port), `close_session` ở finally,
  `running_backend=shardbrowser`.
- `pyproject.toml` — extra `farm = ["shardx==2.1.7"]` (optional dep; core
  install không kéo shardx).

## Pin list (C7 — upgrade = rebuild)

| Thành phần | Pin | Ở đâu |
|---|---|---|
| Base image | `python:3.12.7-slim-bookworm` | Dockerfile FROM |
| Node.js | `22.11.0` + sha256 x64/arm64 | ARG + verify trong build |
| OpenCLI | `@jackwener/opencli@2.0.3` (= `tested` trong tools.py) | ARG, `npm i -g --ignore-scripts` |
| shardx | `2.1.7` | ARG + extra `farm` + `SHARDX_VERSION` trong backend |
| Chromium patched | engine `158.0.8127.36`, tải lúc build (`Runtime().install()`) | `python -m agent_reach.backends.shard install` |
| OpenCLI extension | release tag `v2.0.3` → `/opt/farm-seed/extension` | build-time fetch, không `releases/latest` |
| agent-reach source | build arg `AGENT_REACH_SOURCE_SHA` | ENV trong image + README |

## Auto Run Result

- Unit/static: `pytest tests/test_container_template.py` — **11/11 pass**
  (pin lines, không `latest`, không unpinned pip/npm, compose mem_limit +
  env wiring, entrypoint check+exec, shard launch args đúng verdict,
  no-sandbox chỉ khi env, CDP timeout đóng session, NEED_SETUP khi thiếu shardx).
- Regression: `pytest tests/test_browser.py test_browser_slots.py
  test_manage.py test_tools_install.py` — 96/96 pass.
- Full suite: **1009 passed** (trước vòng fix shard cuối; diff sau đó chỉ
  đụng `backends/shard.py` + test file này — 11/11 pass lại).
- **Docker live-verify trên host này (Docker 29.8.1, macOS arm64, image
  `linux/amd64` qua emulation) — ĐÃ CHẠY, không pending:**
  - `docker build --platform linux/amd64` thành công; layer install in
    `{"installed": true, "executable": "/home/farm/.cache/shardx-sdk/ShardX-Linux/chrome", "engine": "158.0.8127.36"}`.
  - `agent-reach check` trong container: JSON hợp lệ `schema_version: 1`,
    20 results, 13 passed / 7 NEED_SETUP (kênh cần credential — đúng kỳ vọng
    image sạch); có `REACH_ACCOUNT` chưa đăng ký → `unknown farm account`
    (gate farm, đúng hành vi).
  - `agent-reach browser setup`: `ok: true`, `backend: shardbrowser`,
    `running: true`, `opencli_connected: true`, `opencli_context: g2e4n7jk`,
    `extension_version: 1.0.27`. `/json` trong container có page `ShardBrowser`
    + service_worker `chrome-extension://…/background.js`.
  - Cô lập bridge: `curl 127.0.0.1:9222` từ host → không tới được (CDP nằm
    trong netns container).
  - RAM cap: `docker stats` = `717.7MiB / 1.5GiB`, cgroup
    `memory.max` = `1610612736` (1536m) — enforce đúng.
  - `agent-reach twitter post '<text>'`: tới gate `NEED_SETUP unknown farm
    account` — đường lệnh chạy được; post thật cần test account đã login
    (pending operator, không phải lỗi template).

### Live verify log
- 2026-11-22: build amd64 + run `farm-verify142` (`--memory=1536m`,
  `FARM_BOOT_CHECK=0`, volume `farm-verify142-home`) → check/setup/status/
  stats như trên; container + volume đã `rm` sau verify.

## Deviations

- Backend đặt tại `agent_reach/backends/shard.py` (không phải
  `runtime/backends/` như ticket gợi ý) — seam thật của cloak/opencli nằm ở
  `agent_reach/backends/`.
- `x post` trong ticket = channel `twitter`: lệnh thật là
  `agent-reach twitter post '<text>'`.
- `REACH_BROWSER_NO_SANDBOX=1` trong container: Chromium sandbox cần userns
  kernel mà container thường thiếu; biên isolation của farm là proxy+netns.
- Volume account: named volume `farm-<account>_home` qua compose project
  (không bind-mount `./accounts/` — tránh xung đột UID giữa host và uid 1000).
- `apt` deps của Chromium không pin từng gói (apt không hỗ trợ digest) —
  anchor pin là tag base image; ghi rõ trong Dockerfile.

## Risks / pending

- **Proxy residential thật** + pass anti-fraud X: pending từ 14.1 (operator
  cung cấp proxy thật ở 14.2/14.6).
- **Extension↔daemon bind trên ShardX**: service_worker hiện diện đã verify
  ở spike; `bind()`/`opencli_context` trên shard session verify khi chạy
  container thật (cùng cơ chế Cloak, low risk).
- API shardx 2.1.7 đã đối chiếu source wheel (không còn suy đoán):
  `ShardX(profiles_dir=).create_profile(template)` → `launch(profile, proxy=,
  cdp=, headless=, extra_args=)`; id profile đóng băng tại
  `<profile_dir>/shardx-profile-id`, relaunch qua `open_profile()`.
  `Runtime.install()` trả None (thành công = binary tồn tại), cache thật nằm ở
  `~/.cache/shardx-sdk`, engine unzip bằng `unzip` hệ thống (đã thêm apt).
- Không đụng `dispatch.py`/`farm.py`/`test_farm_*.py` (lane agent khác đang sửa).
- Story 14.3 sẽ đổi `network_mode` sang netns riêng + kill-switch; template
  này giữ bridge mặc định và ghi chú trong compose/README.
