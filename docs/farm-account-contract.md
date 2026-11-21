# Farm account contract — identity, kill-switch, ledger, errors

**Status: binding contract.** The semantics below are decided once here
(epic-account-substrate, R3) so that epic-write-lanes, epic-account-guardrails,
epic-fleet-orchestration and epic-farm-resilience read and write the same rules
instead of each inventing their own. §6 marks what is already implemented versus
what is contract-only pending its owning epic.

Normative language: **must** = required for correctness, **must not** =
contract violation, **should** = strong default.

---

## 1. Identity per run

Every farm action run executes under exactly one account identity, resolved
before dispatch:

```
REACH_ACCOUNT=<name>  →  registry lookup  →  env applied to the run
```

- `REACH_ACCOUNT` is the selector env var (epic decision 2026-11-21; a `--account`
  CLI flag may set it later — the variable, not the flag, is the contract).
- Resolution reads the `accounts` row from the registry
  (`~/.agent-reach/farm/registry.db`, see §5) and yields:

  | Env var | Value |
  |---|---|
  | `HOME` | the account's home dir (`farm/accounts/<name>/`, stored in `home`) |
  | `REACH_BROWSER_PROXY` | the account's proxy URL (stored in `proxy`) |
  | `REACH_ACCOUNT` | `<name>` echoed back |

  `HOME` points sessions, state, browser profile and the registry itself at the
  account's own tree — per-account state falls into the right place with no
  code-level partitioning.

- **Failure rule:** `REACH_ACCOUNT` set to a name with no registry row →
  `NEED_SETUP` (suggest `agent-reach farm account add`). The run **must not**
  silently fall back to the default `HOME` — a run that asked for account_i and
  got the default identity leaks activity onto the wrong account.
- `REACH_ACCOUNT` unset → the legacy single-account path is unchanged (default
  `HOME`, global proxy config). Farm identity is strictly opt-in per run.
- Account names are the registry's validated form:
  `[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}` (enforced at `farm account add`).

## 2. Kill-switch — two layers

**Contract semantics (defined once, here):** *zero egress outside proxy_i —
fail-closed.* Any network egress from an account run that bypasses that
account's proxy is a contract violation. When proxy_i's usability cannot be
confirmed the run does not proceed; it **must not** fall back to direct egress
(home IP). This applies to read actions too — reads leak the IP just the same.

This is deliberately **not** "verify exit IP == proxy IP": residential proxies
rotate exit IPs, so an IP-equality check false-positives against a healthy
proxy. The invariant is about the egress *path*, not the observed address.

| Layer | Mechanism | Strength | Status |
|---|---|---|---|
| 1 — dispatch soft-check | Pre-flight at dispatch: proxy configured + non-empty + matches registry row, then a TCP handshake probe (5s, verdict cached ~60s). Failure → `PLATFORM_BLOCKED`, never direct. | Best-effort: catches misconfiguration and dead proxies at the application layer; cannot stop a kernel-level leak. | Implemented — `_enforce_proxy_kill_switch` (story 13.2) |
| 2 — netns hard-enforce | Container/netns routing makes egress outside proxy_i impossible at the network layer. | Hard: the leak path does not exist. | Implemented — `farm-netns-killswitch` iptables rules (story 14.3) |

Layer 1 without layer 2 was accepted risk during the env-injection phase
(epic constraint C1). Both layers are now shipped: the soft-check is the
pre-flight guard, the container substrate (story 14.3) is the hard enforcement.

## 3. Ledger — rolling 24h, multi-writer

Every account write action appends to `actions_log` in the registry DB
(schema §5). The ledger is the only counting source for per-account quotas.

- **Rolling window:** counts are per `(account, action_class)` over the last
  86 400 s (`ts >= now - 86400`) — a sliding window, not calendar days.
- **Only success counts toward quota.** `outcome = 'success'` rows are the cap
  counter; every other outcome (error codes from `result.py`) is an *attempt*.
  Attempts are reported as a separate counter and never burn an innocent
  account's daily cap during an upstream outage.
- **Fallback writes also record.** Actions executed on the fallback lane
  (epic-farm-resilience) **must** write `actions_log` rows the same way —
  the ledger is per-account, not per-lane, so caps hold across a lane switch.
- **Concurrency model:** the DB runs `journal_mode=WAL` — concurrent readers,
  serialized writers (dispatch, fleet manager, drift monitor, fallback lane are
  all legitimate writers). Rules for writers:
  - one short-lived transaction per `record_action` call — never hold a write
    transaction open across upstream I/O;
  - set `PRAGMA busy_timeout` (≥ 5 000 ms); on `SQLITE_BUSY` after the timeout,
    retry a small bounded number of times, then surface `UPSTREAM_BROKEN`;
  - one connection per call, opened via `farm.connect()` — the helper already
    enforces WAL, symlink-safety and `0600` perms.
- Registry files and account dirs are created through
  `make_private_dir`/`ensure_no_symlink_path` — the same privacy model as
  `state.py`/`sessions.py` (0700 dirs, 0600 db, no symlink following).

## 4. Error class `PERSISTED_QUERY_DEAD` and the fallback route rule

`PERSISTED_QUERY_DEAD` is a first-class error code in `result.py` alongside
`RATE_LIMITED`/`PLATFORM_BLOCKED`/etc. It is raised when a lane detects that the
persisted-query doc-id / upstream GraphQL schema it replays has drifted
(query-not-found, schema-mismatch, or equivalent upstream-schema signals).

- **Same-lane finality:** a dead persisted query does not un-die — retrying the
  identical replay on the same lane is prohibited. The error is final *for that
  lane*, not for the action.
- **Route rule:** a run that fails with `PERSISTED_QUERY_DEAD` is routed once to
  the fallback lane (XActions HTTP lane, initiative R12) *for that account*.
  Implemented by epic-farm-resilience (stories 16.1/16.2, `xactions_lane.py`);
  this document fixes the rule.
- **Fallback constraints:** fallback egress **must** go through the same
  account's proxy (no home-IP leak — initiative C3), and fallback write attempts
  **must** be recorded in the ledger (§3).
- Existing semantics are unchanged: `RATE_LIMITED` still means "stop and respect
  the cooldown", `PLATFORM_BLOCKED` still means "stop, human must resolve" —
  `PERSISTED_QUERY_DEAD` adds a third, lane-scoped terminal signal.

## 5. Schema

Registry: SQLite (WAL), `~/.agent-reach/farm/registry.db` — the single source
of truth mapping `account ↔ proxy ↔ home ↔ age ↔ ledger ↔ ban_status`.

Implemented now (`agent_reach/runtime/farm.py`, story 11.1):

```sql
CREATE TABLE IF NOT EXISTS accounts (
  name       TEXT PRIMARY KEY,                -- validated [a-zA-Z0-9][\w-]{0,31}
  proxy      TEXT NOT NULL,                   -- http|https|socks5 URL
  home       TEXT NOT NULL,                   -- farm/accounts/<name>/ (0700)
  age        INTEGER NOT NULL,                -- onboard unix epoch; warm-up epic reads this
  ban_status TEXT NOT NULL DEFAULT 'new',     -- lifecycle field (see below)
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
```

Contract-defined here, implemented by story 11.5:

```sql
CREATE TABLE IF NOT EXISTS actions_log (
  account      TEXT NOT NULL REFERENCES accounts(name),
  ts           INTEGER NOT NULL,              -- unix epoch of the attempt
  action_class TEXT NOT NULL,                 -- e.g. post, reply, like, follow
  outcome      TEXT NOT NULL                  -- 'success' | result.py error code
);
-- recommended index: (account, action_class, ts)
```

- `outcome` vocabulary: `'success'`, or any error code `result.py` can emit
  (`RATE_LIMITED`, `PLATFORM_BLOCKED`, `PERSISTED_QUERY_DEAD`,
  `UPSTREAM_BROKEN`, `TIMEOUT`, `NEED_LOGIN`, ...). Anything ≠ `'success'` is an
  attempt, not a quota charge.
- `ban_status` is free-form lifecycle text starting at `'new'`; transition
  automation is deferred (§6) — writers must treat unknown values as opaque and
  must not invent transitions ad hoc.
- `age` is populated at `farm account add` and is read-only afterward; the
  warm-up schedule (epic-account-guardrails) consumes it.

## 6. Contract vs deferred

| Item | Status | Owner |
|---|---|---|
| `accounts` table, WAL registry, `add`/`list`, account HOME skeleton | Implemented | story 11.1 (`farm.py`) |
| Identity env contract (`REACH_ACCOUNT` → `HOME`/`REACH_BROWSER_PROXY`) | Contract — bound here | stories 11.3 (`env`), 11.4 (dispatch) |
| `NEED_SETUP` on unknown account, no silent default `HOME` | Contract — bound here | story 11.4 |
| Kill-switch semantics "zero egress outside proxy_i, fail-closed" | Contract — bound here | this doc is the single definition |
| Layer-1 dispatch soft-check | Implemented | story 13.2 (`dispatch._enforce_proxy_kill_switch`) |
| Layer-2 netns hard-enforce | Implemented | story 14.3 (`farm/container/netns-killswitch.sh`) |
| `actions_log` schema, `record_action`, rolling-24h query, `ledger` CLI | Contract — bound here | story 11.5 |
| `PERSISTED_QUERY_DEAD` error class in `result.py` | Contract — bound here | story 11.6 |
| PQD → fallback routing, fallback lane wiring | Implemented | stories 16.1/16.2 (`xactions_bridge.py`, `xactions_lane.py`) |
| Caps / warm-up enforcement reading the ledger | Implemented | stories 13.1 + 13.3 (`dispatch._enforce_write_quota`) |
| `ban_status` transition automation | **Deferred** | epic-fleet-orchestration |
| Container template, per-account netns/bridge bind | Implemented | stories 14.2 + 14.3 (`farm/container/`) |

### Implemented — dispatch guardrails (stories 13.1–13.3)

Both guards run inside `run_action` before cooldown, public-web, and every
backend — kill-switch first, then quota — so a refusal is zero egress and
writes no ledger row. A refusal surfaces as a normal JSON envelope:
`ok=false`, `error.code` = `PLATFORM_BLOCKED` / `QUOTA_EXCEEDED`,
`error.message` naming the account, verb, cap, and count.

- **Kill-switch** (`dispatch._enforce_proxy_kill_switch`): applies to every
  action (reads included) under `REACH_ACCOUNT` with a registry proxy. A dead
  proxy → `PLATFORM_BLOCKED`. `REACH_PROXY_CHECK=0` bypasses;
  `REACH_PROXY_CHECK_TTL` overrides the 60s verdict cache.
- **Write quota** (`dispatch._enforce_write_quota`): only verbs in
  `WRITE_ACTIONS` (twitter post/reply/quote/thread, the facebook write set)
  count; reads and unlisted verbs never touch the ledger. Caps default in
  `DEFAULT_CAPS`; override with `REACH_CAP_<CLASS>` (`post-edit` →
  `REACH_CAP_POST_EDIT`). `0` refuses every write; a class with no entry is
  unlimited. Over cap → `QUOTA_EXCEEDED`, message names account/class/cap/count.
- **Warm-up ramp**: the resolved cap scales by the registry `age` onboard
  epoch — d0–3: 25%, d4–7: 50%, d8–14: 75%, d15+: 100% (`RAMP_SCHEDULE`,
  overridable via `REACH_RAMP="days:factor,..."`). Effective cap =
  `max(1, floor(cap × factor))`; a missing epoch means mature (factor 1.0) and
  cap `0` stays refuse-all. When a cap is ramped the refusal message shows
  `cap × ramp (account age Nd) = effective`.

The operator-facing runbook is `docs/farm-guardrails-playbook.md`.

## References

- Epic: `_bmad-output/initiative-multi-account-farm/tickets/epic-account-substrate/epic-account-substrate.md` (R3)
- Architecture: `docs/multi-account-farm-architecture.md` §3 (container-per-account), §4b #7, §5 (kill-switch, chain-ban)
- Code: `agent_reach/runtime/farm.py` (registry), `agent_reach/runtime/result.py` (error envelope), `agent_reach/utils/paths.py` (privacy model)
- Initiative decisions: `_bmad-output/initiative-multi-account-farm/initiative-multi-account-farm.md` (kill-switch semantics, fallback-via-proxy, PQD route, WAL multi-writer)
