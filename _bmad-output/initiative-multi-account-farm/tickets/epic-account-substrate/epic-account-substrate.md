---
id: 11
type: epic
title: "Per-account substrate + contracts"
parent: initiative-multi-account-farm
covers: ["R15"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: true
risk: medium
---

# Per-account substrate + contracts

## Description

Platform-baseline epic — thứ mà R5 đến R11 đều ngầm giả định nhưng doc gốc chưa liệt thành work item: một per-account registry là source of truth duy nhất (account ↔ proxy ↔ container ↔ session ↔ tuổi account ↔ rolling-24h ledger ↔ ban status), contract danh tính per-run ("run này là account_i với proxy_i"), và kill-switch contract viết đúng semantics một lần ở một chỗ: **zero egress ngoài proxy_i — fail-closed** (KHÔNG phải "verify exit IP = proxy", vì residential proxy rotate IP).

## Outcome

Caps, warm-up, kill-switch, fleet health và fallback — 5 epic sau — đọc-ghi cùng 1 format duy nhất; kill-switch có đúng 1 chỗ định nghĩa thay vì mỗi epic tự nghĩ một semantics.

## Requirements

- R1 (parent R15): Registry SQLite (WAL mode) tại `~/.agent-reach/farm/registry.db` là source of truth duy nhất — schema mapping `account ↔ proxy ↔ container ↔ session(HOME) ↔ age ↔ ledger ↔ ban_status`; account HOMEs tại `~/.agent-reach/farm/accounts/<name>/` theo đúng pattern `make_private_dir`/`home_dir` sẵn có.
- R2 (parent R15): CLI `agent-reach farm account` — `add <name> --proxy <url>` (tạo HOME skeleton + row, `age` = onboard timestamp, `ban_status = new`), `list` JSON, `env <name>` trả env dict resolve được, `ledger <name>` JSON.
- R3 (parent R15): Contract doc `docs/farm-account-contract.md` định nghĩa: identity per-run (`REACH_ACCOUNT` → registry → env), kill-switch 2 lớp (dispatch soft-check = best-effort pre-flight; netns hard-enforce = epic-fleet-orchestration bake), ledger rolling-24h + concurrency model cho đa-writer (WAL; chỉ đếm success — attempts counter tách; fallback writes bắt buộc ghi ledger), error class `PERSISTED_QUERY_DEAD` + rule route vào fallback lane.
- R4 (parent R15): Dispatch resolve — `REACH_ACCOUNT=<name>` → registry lookup → env áp vào action run (`HOME_i` để session/state rơi đúng dir, `REACH_BROWSER_PROXY_i`); account thiếu/unknown → `NEED_SETUP` rõ ràng, không silent dùng HOME mặc định.
- R5 (parent R15): Ledger — `record_action(account, action_class, outcome)` ghi vào registry DB; query rolling-24h per account per action-class; chỉ outcome `success` đếm vào cap, attempts nằm counter riêng.
- R6 (parent R15): `PERSISTED_QUERY_DEAD` là error class trong `result.py` (envelope mapping đủ), raise ở error path khi nhận persisted-query/upstream-schema signal theo contract.
- R7 (parent R15): Verify pytest e2e — 2 account giả trên 2 HOME giả lập chạy tuần tự: `env` resolve tách biệt, dispatch ghi state đúng HOME, ledger đếm đúng per account.

## Constraints

- C1 (parent C1): Phase này là env-injection host-level — KHÔNG cần container/netns; isolation mechanism phải định nghĩa sẵn trong contract để epic-fleet-orchestration bake (HOME + bridge bind per-account).
- C3 (parent C3): Proxy per-account lưu trong registry; semantics "zero egress ngoài proxy_i" ghi trong contract — enforce ở epic sau, contract định nghĩa trước.
- Stdlib-only: `sqlite3` có sẵn, không thêm dependency mới (ponytail — registry là tảng mọi epic sau đứng lên, càng ít moving part càng tốt).
- Mọi file/dir registry tạo qua `make_private_dir`/atomic write — kế thừa privacy model của `state.py`/`sessions.py`.

## Done when

- Registry tồn tại (file/DB chọn lúc refine — đơn giản nhất thắng): CRUD account, lưu mapping proxy/container/session/age/ledger/status; field `age` được populate lúc onboard (warm-up của epic 13 đọc nó); có schema doc.
- Contract doc trong `docs/` định nghĩa: identity per-run, kill-switch 2 lớp (dispatch soft-check = best-effort pre-flight; netns hard-enforce = epic-fleet-orchestration bake), format ledger rolling-24h, **concurrency/locking model cho ledger đa-writer** (dispatch, fleet manager, drift monitor, fallback lane — vd SQLite WAL/transaction); **ledger chỉ đếm action thành công — attempt tách counter riêng** để outage không đốt caps của account vô tội; **fallback writes (epic-farm-resilience) cũng bắt buộc ghi ledger**; error class `PERSISTED_QUERY_DEAD`/upstream-schema và rule route vào fallback.
- Dispatch resolve được account_i → env_i/proxy_i từ registry (không cần container — env injection đủ cho phase này).
- Verify: tạo 2 account giả trên 2 HOME giả lập, chạy tuần tự → mỗi run resolve đúng proxy/env riêng. Song song thật chỉ khi contract đã định nghĩa isolation mechanism (HOME + bridge bind per-account) — hard-isolation verify nằm ở Done-when epic-fleet-orchestration.

## Boundaries

- Out: write lanes (epic-write-lanes), enforcement logic caps/warm-up (epic-account-guardrails), container template/netns (epic-fleet-orchestration — chỉ định nghĩa contract env nó sẽ populate), metrics.
- Dependencies authoritative: `tickets/tickets.toml`.
- Must not change: `_pace_browser`/cooldown sẵn có; bridge contract.
- Touch point: `agent_reach/runtime/dispatch.py` (hook đọc registry trước khi dispatch), `docs/` (contract doc), `agent_reach/runtime/session_reuse.py` (session field của registry).

## References

- parent — `initiative-multi-account-farm.md`, Requirement R15
- architecture — `docs/multi-account-farm-architecture.md` §3 (container-per-account), §5 (kill-switch, chain-ban)
- code — `agent_reach/runtime/dispatch.py`, `agent_reach/runtime/session_reuse.py`, `agent_reach/runtime/browser_watcher.py`

## Notes

- Decision: 2026-11-21 — registry = SQLite WAL tại `~/.agent-reach/farm/registry.db` (stdlib `sqlite3`, đáp multi-writer contract; JSON+lock bị loại vì 4 writer concurrent); account HOMEs `accounts/<name>/` — mở rộng pattern `session_env`/`make_private_dir` sẵn có.
- Decision: 2026-11-21 — account selection bằng env `REACH_ACCOUNT` (khớp env-injection hiện có); CLI flag `--account` để khi cần.
- Decision: 2026-11-21 — tracer bullet = entry 1 `farm-registry-scaffold` (thinnest path CLI → registry → fs); contract doc đứng ngay sau vì 5/6/8 implement theo semantics nó định nghĩa; e2e đóng epic sau sweep.
- Decision: 2026-11-21 — ban_status automation và netns hard-enforce defer sang epic-fleet-orchestration; epic này chỉ field + contract.

## Code Review

### 2026-11-21

**Code review complete.** 0 `decision-needed`, 10 `patch`, 3 `defer`, 1 rejected. (4 lenses: blind-hunter, edge-case-hunter, verification-gap, intent-alignment; triage tại `/tmp/bmad_triage_epic11.md` — 14+6+2 raw findings → 12 root-cause findings.)

- [x] [Review][Patch] Registry/farm state resolve theo HOME động — farm.* call trong run window (nested `run_action` qua `public_web.attempt`) mở registry rỗng thứ hai dưới account HOME → NEED_SETUP + stray `farm/accounts/<a>/.agent-reach/farm/` [agent_reach/runtime/farm.py:48-86, agent_reach/runtime/dispatch.py:221-260]
- [x] [Review][Patch] Env-region hazards: `REACH_ACCOUNT` selector + `account_env` resolve đọc ngoài `_ACCOUNT_ENV_LOCK` (race cross-account) + `os.environ` mutation raise → lock leak → mọi run sau BUSY [agent_reach/runtime/dispatch.py:231-257]
- [x] [Review][Patch] Farm exceptions (sqlite3.Error non-"locked", PrivatePathError, OSError) thoát `run_action`/`_mgmt` dạng traceback — vỡ contract một-dòng-JSON [agent_reach/runtime/farm.py:190-201, agent_reach/runtime/dispatch.py:290-294, agent_reach/runtime/cli.py:83-90]
- [x] [Review][Patch] WAL/SHM sidecar files không được chmod 0o600 — chỉ registry.db chính [agent_reach/runtime/farm.py connect]
- [x] [Review][Patch] PQD heuristic quá rộng (`"document"+"not found"` substring) + đứng trước login/session check → auth failure bị misclassify thành PERSISTED_QUERY_DEAD [agent_reach/channels/_facebook_write.py:275-280]
- [x] [Review][Patch] `_validate_proxy` không bắt buộc port — `http://proxyhost` pass validation [agent_reach/runtime/farm.py]
- [x] [Review][Patch] `add_account` để lại home dir mồ côi khi INSERT bị IntegrityError (tên trùng) [agent_reach/runtime/farm.py]
- [x] [Review][Patch] Index drift vs contract §5 `(account,ts)` vs `(account,action_class,ts)` + `PRAGMA foreign_keys` không bật + `record_action` accept mọi outcome string [agent_reach/runtime/farm.py]
- [x] [Review][Patch] `account_env` trả home không re-validate symlink lúc resolve [agent_reach/runtime/farm.py]
- [x] [Review][Patch] Dead choices `farm env`/`farm ledger` top-level stub "not implemented yet" [agent_reach/runtime/cli.py]
- [x] [Review][Defer] `PERSISTED_QUERY_DEAD ∈ FINAL_CODES` mâu thuẫn contract §4 (final-per-lane + fallback-eligible) — deferred: lane-finality routing thuộc epic-farm-resilience, chưa có fallback infra [agent_reach/runtime/result.py]
- [x] [Review][Defer] `record_action` không có production caller — ledger không được feed bởi run thật — deferred: owned by epic-write-lanes ticket 12.7 + epic-account-guardrails per contract §6 [agent_reach/runtime/farm.py]
- [x] [Review][Defer] Orphan backend thread post-TIMEOUT đọc HOME/proxy của account kế tiếp — deferred: pre-existing `_call_with_deadline` design, cần env-snapshot per-worker → epic-account-guardrails [agent_reach/runtime/dispatch.py:333-377]

#### Rejected

- `farm account list` trên máy sạch tạo `~/.agent-reach/farm/` + registry.db rỗng (read-path side effect) — consistent với `state.root()` pattern toàn codebase, rarely visible, fix adds complexity.
