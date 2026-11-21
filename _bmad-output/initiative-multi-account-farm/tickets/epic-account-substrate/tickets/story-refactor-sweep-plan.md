---
title: 'Refactor sweep'
type: refactor
ticket: '7'
created: '2026-11-21'
status: built
baseline_revision: f39ac097dffee8d96dffbb0e518dd239eb2cf75d
route: oneshot
route_source: pinned
risk: low
review: none
review_source: pinned
lenses_ran: []
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/farm-account-contract.md'
warnings: []
deferred:
  - 'actions_log index (account,ts) -> contract-recommended (account,action_class,ts): contract ghi "recommended" (non-binding); query thực tế WHERE account,ts GROUP BY class được index hiện tại phục vụ đúng. Đổi index = schema drift (DB cũ giữ index cũ, IF NOT EXISTS không drop) hoặc thêm index thứ 2 = write amplification cho query shape chưa tồn tại. Defer tới epic-account-guardrails khi per-class query thật sự xuất hiện.'
  - 'WAL multi-writer lock-contention test thật (residual 11.5): cần harness contention riêng, là test infra mới — không phải diff cleanup.'
  - 'Contract §6 deferred items (netns layer-2, PQD->fallback routing, caps/warm-up, ban_status automation): chủ sở hữu epic-fleet-orchestration / epic-farm-resilience / epic-account-guardrails — ngoài scope epic này.'
---

<intent-contract>

## Intent

**Problem:** Epic-account-substrate kết thúc với một inconsistency đã được
ghi nhận trong review/residual của story 11.5: `list_accounts` và
`account_env` (và writer `add_account`) bypass `_with_busy_retry` — DB locked
quá `busy_timeout` raise raw `sqlite3.OperationalError`, crash CLI thay vì
envelope `UPSTREAM_BROKEN` (đúng defect `medium` đã patch cho `ledger_24h`
trong review 11.5). Sweep này đồng nhất mọi registry op đi qua helper.

**Scope chốt lúc start** (từ Review Triage Log / Auto Run Result của 6 plans +
contract §3/§5/§6 — verify lại từng candidate trên code):

- IN: wrap `_with_busy_retry` cho `add_account` (writer — contract §3 bind
  bounded BUSY retry), `list_accounts`, `account_env` (residual 11.5). Zero
  new behavior: cùng helper, cùng envelope, cùng `closing(connect())` per call.
- IN: 1 test locked-DB → `UPSTREAM_BROKEN` cho cả 5 public ops (giữ
  consistency bằng monkeypatch `farm.connect`, không cần contention thật).
- OUT (deferred, xem frontmatter): index `(account, action_class, ts)`,
  contention test thật, mọi mục contract §6 thuộc epic khác.

### Always

- Chỉ `agent_reach/runtime/farm.py` + `tests/test_farm.py`. Không đổi signature,
  schema, CLI, dispatch.
- `add_account`: giữ nguyên semantics IntegrityError→BAD_INPUT (không rmtree
  skeleton của account đang tồn tại) và rmtree-chỉ-khi-vừa-tạo trên failure
  khác — cleanup chạy NGOÀI closure retry để BUSY tạm thời không xóa skeleton.
- `account_env`/`list_accounts`: query body bọc `_with_busy_retry` như
  `ledger_24h`, `what` mặc định.

### Never

- Không đổi index/schema `actions_log`, không DROP/CREATE INDEX mới.
- Không thêm behavior mới (không API mới, không option mới, không retry policy
  khác `_BUSY_RETRIES=3`).
- Không đụng `dispatch.py`, `cli.py`, contract doc.

## Tasks & Acceptance

1. `farm.py`: `add_account` tách `_insert()` closure → `_with_busy_retry`;
   `list_accounts`/`account_env` query → closure + `_with_busy_retry`.
2. `tests/test_farm.py`: `test_locked_db_surfaces_upstream_broken` — patch
   `farm.connect` raise `OperationalError("database is locked")`, assert cả 5
   ops → `ReachError(UPSTREAM_BROKEN)`.
3. `uv run --extra dev python -m pytest tests -x -q` — baseline 868 pass.

## Verification

- `uv run --extra dev python -m pytest tests -x -q` — expected 869 pass.
- `git diff` chỉ farm.py + test_farm.py (+ plan file này).

## Auto Run Result

**Scope chốt:** busy-retry consistency — `add_account`, `list_accounts`,
`account_env` giờ đi qua `_with_busy_retry` như `record_action`/`ledger_24h`
(contract §3 bounded BUSY retry → `UPSTREAM_BROKEN`; residual 11.5 đã defer
đúng chỗ này). `add_account` cleanup rmtree được giữ NGOÀI closure retry —
BUSY tạm thời không xóa skeleton; IntegrityError→BAD_INPUT giữ nguyên (không
rmtree dir của account tồn tại sẵn). Semantics đổi duy nhất: DB locked quá
timeout trước đây crash traceback → giờ envelope `UPSTREAM_BROKEN` sau 3
retry — cùng defect class review 11.5 đã patch cho `ledger_24h`, không phải
behavior mới.

**Excluded (defer, chi tiết frontmatter):** index `actions_log`
`(account,action_class,ts)` — "recommended" non-binding, query hiện tại được
`(account,ts)` phục vụ đúng, đổi = schema drift hoặc write amplification cho
query shape của epic-account-guardrails; WAL contention test thật — test
infra mới; contract §6 deferred items thuộc epic khác.

**Files changed:** `agent_reach/runtime/farm.py` (3 ops bọc
`_with_busy_retry`, +28/-13), `tests/test_farm.py` (+1 test
`test_locked_db_surfaces_upstream_broken` param cả 5 ops qua monkeypatch
`farm.connect`, +import `UPSTREAM_BROKEN`).

**Verification:** `pytest tests -x -q` → **869 passed** (868 baseline + 1).
Diff không chạm dispatch/cli/schema — zero new behavior.
