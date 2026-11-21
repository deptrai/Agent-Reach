---
title: 'Farm registry scaffold'
type: feature
ticket: '1'
created: '2026-11-21'
status: built
baseline_revision: f2c7a142a33534a41d14e3db5e98e74c282bcb11
route: full
route_source: auto
risk: medium
review: quick
review_source: pinned
lenses_ran: [quick]
deferred: []
context: []
warnings: []
---

## Review Triage Log

### 2026-11-21 — Review pass 1 (quick)

- **[medium][patch]** `urlparse` raise `ValueError` (invalid IPv6/NFKC) → crash non-JSON; bad port (`:99999`/`:abc`) lọt vì `.port` không access — wrap try/except + validate port.
- **[medium][patch]** `registry.db` (file) không qua `ensure_no_symlink_path` — check trước `connect`.
- **[medium][patch]** `state.root()` 0755 trên HOME mới + `registry.db` 0644 — `make_private_dir(state.root())` + chmod 0600 db.
- **[low][patch]** skeleton+no-row seam (order theo plan): cleanup rmtree on non-IntegrityError khi vừa tạo.
- **[low][patch]** WAL pragma không verify result — assert "wal" else UPSTREAM_BROKEN.
- **[low][patch]** test gaps: perms state.root, proxy ValueError/port, extra positional args.
- Seams verified clean: SQLi parameterized, name regex chặn traversal trước fs, CLI route `farm` đúng chỗ, schema khớp spec, IntegrityError→BAD_INPUT, envelope JSON 1 dòng, no fail-open, no secrets.

## Intent Contract

`agent-reach farm` is the control surface of the multi-account farm initiative.
Story 1 is the tracer bullet: a SQLite-WAL registry at
`~/.agent-reach/farm/registry.db` plus the first two commands —
`farm account add <name> --proxy <url>` (creates the account HOME skeleton +
row) and `farm account list` (JSON rows). Every later epic reads/writes this
registry; it must be stdlib-only and respect the existing privacy model.

### Always

- New module `agent_reach/runtime/farm.py` — `sqlite3` stdlib only, no new deps.
- Registry path under `state.root()` (`~/.agent-reach/farm/`): use
  `make_private_dir` / `ensure_no_symlink_path` from `agent_reach/utils/paths.py`
  and `state.root()` (honors `HOME` → tests can isolate).
- DB opened with `PRAGMA journal_mode=WAL`, schema created
  `CREATE TABLE IF NOT EXISTS`, one connection per call (context manager).
- `farm account add`: validate name `[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}` and proxy
  URL (`urlparse` scheme+host, schemes http|https|socks5); create HOME skeleton
  `accounts/<name>/` via `make_private_dir`; insert row
  `(name, proxy, age, ban_status, created_at, updated_at)` —
  `age` = onboard unix epoch, `ban_status = "new"`; duplicate name →
  `ReachError(BAD_INPUT)`.
- `farm account list`: return all rows as JSON-safe dicts.
- CLI wiring: `_cmd_farm` in `agent_reach/runtime/cli.py` with `_Parser` (same
  pattern as `_cmd_browser`), routed `if head == "farm":` in `main()`,
  output via `_mgmt`/`_emit` envelope (`{"ok":true,"action":"farm account add","result":{...}}`).

### Never

- No read/write action dispatch changes; no container/netns work (later epics).
- No ledger table in this story (R5 story 11.5 owns it); schema only `accounts`.
- No credential/session copying into account HOME — skeleton dir only.
- Don't touch `_pace_browser`, `state.save`, browser contracts.

### I/O & Edge-Case Matrix

| Case | Input | Expected |
|---|---|---|
| happy add | `farm account add test1 --proxy http://p1:8080` | ok:true, result{name,proxy,age,ban_status:"new",home} |
| list | after add | `accounts:[{...}]` with all fields |
| duplicate | add same name twice | BAD_INPUT, second call fails |
| bad name | `farm account add "../x"` or spaces | BAD_INPUT before any fs/DB |
| bad proxy | `--proxy notaurl` (no scheme/host) | BAD_INPUT |
| missing args | `farm account add` | BAD_INPUT |
| unknown op | `farm bogus` | BAD_INPUT |
| HOME isolation | `HOME=/tmp/x` | registry at `/tmp/x/.agent-reach/farm/registry.db`, perms 0700 |

## Code Map

- `agent_reach/runtime/state.py` — `root()` returns `home_dir()/".agent-reach"` (line 39).
- `agent_reach/utils/paths.py` — `make_private_dir` (0700+no-symlink, line 69),
  `home_dir` (line 36), `ensure_no_symlink_path` (line 53).
- `agent_reach/runtime/cli.py` — `_Parser`/`_ArgError` pattern (:100),
  `_mgmt` (:82), `_emit` (:108), `_cmd_browser` (:525) as the template for
  `_cmd_farm`; `main()` dispatch head routing (:556-589).
- `agent_reach/runtime/result.py` — `ReachError`, `BAD_INPUT` (:30), `NEED_SETUP` (:23).
- Existing test layout: `tests/test_manage.py`, `tests/test_cli_encoding.py`
  use `monkeypatch`/`HOME` env for isolation — mirror that.

## Tasks & Acceptance

1. `agent_reach/runtime/farm.py` (~120 LOC): `farm_root()`, `registry_path()`,
   `connect()` (sqlite3 + WAL + `CREATE TABLE IF NOT EXISTS accounts(...)`),
   `add_account(name, proxy) -> dict` (validate → make_private_dir HOME skeleton
   → insert), `list_accounts() -> list[dict]`.
   Schema: `accounts(name TEXT PRIMARY KEY, proxy TEXT NOT NULL, home TEXT NOT NULL,
   age INTEGER NOT NULL, ban_status TEXT NOT NULL DEFAULT 'new',
   created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)`.
   `age` = `int(time.time())` at add.
2. `agent_reach/runtime/cli.py`: `_cmd_farm` (parser ops: `account add|list|env|ledger`
   — only `add`/`list` implemented; `env`/`ledger` → BAD_INPUT "not implemented"
   so later stories extend cleanly) + `if head == "farm":` in `main()` +
   add `"farm"` mention in HELP if a commands list exists.
3. `tests/test_farm.py`: monkeypatched `HOME` → add/list happy path, duplicate,
   bad name, bad proxy, missing args, 0700 dir perms, WAL pragma on, JSON-safe output.

## Implementation Notes

- Keep `farm.py` importable without side effects — `connect()` creates dirs lazily.
- `list` must work with no accounts (empty registry → `accounts:[]`).
- `_mgmt` action labels: `"farm account add"`, `"farm account list"`.

## Verification

```bash
uv run --extra dev python -m pytest tests -x -q          # baseline 794
HOME=/tmp/farmtest1 uv run agent-reach farm account add test1 --proxy http://p1:8080
HOME=/tmp/farmtest1 uv run agent-reach farm account list
ls -la /tmp/farmtest1/.agent-reach/farm/                  # registry.db + accounts/test1
```

Live e2e: add/list on a scratch HOME, verify row fields + 0700 perms.

## Auto Run Result

**Outcome:** `farm account add <name> --proxy <url>` + `farm account list` live đúng spec — SQLite WAL registry tại `~/.agent-reach/farm/registry.db`, skeleton HOME 0700, row đủ 7 fields (name PK, proxy, home, age=epoch, ban_status='new', created_at, updated_at).

**Delivered:** `agent_reach/runtime/farm.py` (136 LOC: `farm_root`, `registry_path`, `connect` WAL+symlink-check+chmod 0600, `add_account`, `list_accounts`, `account_home`, validators); `cli.py` +29 (`_cmd_farm` theo `_cmd_browser`, route `if head == "farm"`); `tests/test_farm.py` 30 tests.

**Review:** pass 1 → 6 findings đều real, patch hết (urlparse ValueError+port validation, symlink db check, perms `.agent-reach` 0700 + db 0600, skeleton+no-row cleanup seam, WAL pragma verify, test gaps). 0 reject/defer.

**Verification:** 824 tests pass (794 baseline + 30). Live e2e: add→list row JSON đủ fields; dirs drwx------; bad name/proxy/duplicate → BAD_INPUT JSON 1 dòng.

**Residual:** `env`/`ledger` stub BAD_INPUT — stories 11.3/11.5. Trên macOS `/tmp` là symlink → verify dùng `/private/tmp` (đúng privacy model).
