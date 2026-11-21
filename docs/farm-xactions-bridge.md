# Farm ↔ XActions identity bridge contract

**Status: binding contract** for the link between the Reach farm registry
(epic-account-substrate, `docs/farm-account-contract.md`) and the XActions
`accountPool` (repo `../XActions`, branch `main-clean`,
`src/scrapers/twitter/http/accountPool.js`). Implemented by story 16.1 in
`agent_reach/runtime/xactions_bridge.py`.

Normative language: **must** = required for correctness, **must not** =
contract violation.

---

## 1. One identity, two stores — no shadow accounts

A farm account lives in the Reach registry
(`~/.agent-reach/farm/registry.db`, table `accounts`). Its X-side session and
lock state live in the XActions pool
(`$XACTIONS_HOME/accounts.db` → `~/.xactions/accounts.db`, table `accounts`).
The bridge's only job is the 1:1 mapping plus two data flows:

- **provision (Reach → XActions):** cookies/session land in the pool entry's
  `cookies` column — the same column `pool.add()` writes. The bridge **must
  not** write credentials anywhere else (no sidecar jars, no copies under the
  account home).
- **ban write-back (XActions → Reach):** when XActions marks a pool entry
  `locked` (dead-account error codes 32/64/89/326 hard-lock, 353 soft-locks
  with `lock_until`), `sync_bans()` folds that into the bridge row and the
  registry `accounts.ban_status` lifecycle field.

The bridge **must not** create a parallel account system: `xactions_link` is a
mapping table only — it stores no cookies, no proxy, no credentials. Anything
that identifies the session belongs to exactly one of the two stores.

## 2. Schema mapping

| Reach registry `accounts` | XActions pool `accounts` | Bridge `xactions_link` |
|---|---|---|
| `name` (`a1`, validated `[a-zA-Z0-9][\w-]{0,31}`) | — | `reach_name` PK, FK → `accounts.name` |
| — | `name` (pool entry id, PK) | `xactions_id` — default `reach:<reach_name>` |
| `proxy` | `proxy` | copied at `link()`; pool is authoritative after |
| `ban_status` (lifecycle, free-form) | `locked` + `lock_reason` + `locked_at`/`lock_until` | `ban_status`, `ban_reason`, `ban_checked_at` mirror the last write-back |
| — | `cookies` (session material, 0600 store) | **never stored** — `provisioned_at` records only that provisioning happened |
| `created_at`/`updated_at` (unix s) | `added_at`/`last_used` (ms, `Date.now()`) | `updated_at` (unix s, float) |

```sql
CREATE TABLE IF NOT EXISTS xactions_link (
  reach_name     TEXT PRIMARY KEY REFERENCES accounts(name),
  xactions_id    TEXT NOT NULL,
  provisioned_at REAL,
  ban_status     TEXT,
  ban_reason     TEXT,
  ban_checked_at REAL,
  updated_at     REAL NOT NULL
);
```

The table lives inside the farm registry DB (single WAL file, single backup
unit) and is created by the bridge on connect — `farm.py`'s own schema list is
untouched.

## 3. Link and provision flow

1. `farm.add_account("a1", proxy)` — registry row, unchanged (epic-11).
2. `bridge.link("a1")` → inserts `xactions_link(a1 → reach:a1)` and, if the
   pool entry does not exist, creates a **locked placeholder** in
   `accounts.db`: `cookies=''`, `locked=1`,
   `lock_reason='awaiting_session'`. The placeholder exists so the mapping is
   concrete, but the real pool's `acquire()` filters `locked` rows — an
   unprovisioned entry can never serve traffic.
   - `link("a1", "existing-id")` attaches to an existing pool entry instead;
    no placeholder is created and the entry is **not** modified.
   - Re-linking is idempotent for the same id; a different id re-points the
     mapping (the old pool entry is left for the operator).
3. `bridge.provision_session("a1", blob)` → normalizes `blob` (cookie header
   string / `[{name,value}]` / `{k:v}` map — the same shapes
   `normalizeCookies()` accepts), **requires** `auth_token` like the real
   `pool.add()`, writes it into the entry's `cookies` column, and unlocks the
   entry (`locked=0`). Return values and logs **must** never carry secret
   material.
4. `bridge.resolve("a1")` → `reach:a1`; `bridge.unlink("a1")` drops only the
   mapping row (the pool entry — which may hold real cookies — is left).

## 4. Ban write-back flow

XActions detects a dead account itself: `createPooledClient` marks the pool
entry `locked` with `lock_reason` like `HTTP 403 on Followers: suspended
(code 64)`. Two paths bring that back to the registry:

- `bridge.record_ban(name, status, reason)` — direct write-back when the
  caller already knows the verdict. Updates `xactions_link.ban_*` (only when a
  link exists — a write-back **must not** fabricate a mapping) and always
  stamps `accounts.ban_status` + `updated_at`.
- `bridge.sync_bans()` — fold pass: for every linked account, read the pool
  entry; `locked` (and not the `awaiting_session` placeholder) → `record_ban(
  name, "banned", lock_reason)`. Idempotent to re-run; unlocked entries are
  untouched.

`ban_status` stays free-form per the account contract (§5): the bridge writes
`"banned"` for pool locks; other lifecycle values remain owned by their epics.
Soft locks (`lock_until` set) are folded the same way — expiry is the pool's
business; a later sync clears nothing by itself, `record_ban` with a cleared
status is the explicit path.

## 5. Store locations

| Store | Path | Opened by |
|---|---|---|
| Reach registry | `~/.agent-reach/farm/registry.db` (or `$REACH_FARM_ROOT`) | `farm.connect()` — WAL, busy_timeout 5000, 0600 |
| XActions pool | `$XACTIONS_HOME/accounts.db` (else `~/.xactions/accounts.db`) | `xactions_bridge._connect_pool()` — same WAL/0600 pattern as `openDatabase()` in `accountPool.js` |

Both are SQLite WAL, so Reach (Python, `sqlite3`) and XActions (Node,
`better-sqlite3`) may hold the pool file open concurrently; writers keep one
short transaction per call and retry `SQLITE_BUSY` a bounded number of times.
The bridge applies the pool DDL `CREATE TABLE IF NOT EXISTS` identically to
`accountPool.js` (including the `lock_until` column `ALTER`), so a
bridge-created store opens cleanly under the real pool and vice versa.

## 6. Doc-id rotate hook (for story 16.2)

The fallback lane (story 16.2) replays X's persisted GraphQL doc-ids. Reach
keeps its ids in `query-ids.json`; XActions keeps `endpoint.url`/query-id
state under `$XACTIONS_HOME`. When either store rotates a doc-id, the other
**must** be updated in the same change — the bridge module is the seam where
that sync lives (a `sync_doc_ids()`-style hook); until 16.2 lands this section
only binds the rule that **one** of the stores is written first and the sync
is deterministic, not ad-hoc per lane.

## References

- XActions pool: `../XActions/src/scrapers/twitter/http/accountPool.js`
  (`createAccountPool`, `accounts`/`rate_limits` DDL, `DEAD_ACCOUNT_CODES`).
- Reach registry: `agent_reach/runtime/farm.py`, contract
  `docs/farm-account-contract.md` (§5 schema, `ban_status` lifecycle).
- Bridge code: `agent_reach/runtime/xactions_bridge.py`; tests
  `tests/test_xactions_bridge.py`.
