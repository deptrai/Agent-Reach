# Farm incident playbook — account suspend · proxy pool down · OpenCLI update broke

Operator runbook for the three farm incidents, one section each: detection
signals → contain → diagnose → recover → prevent. Guardrail mechanics
(caps, ramp, kill-switch verdicts) live in `docs/farm-guardrails-playbook.md`;
identity/pool semantics live in `docs/farm-account-contract.md` and
`docs/farm-xactions-bridge.md`. This page only sequences them.

Every `agent-reach` command prints exactly one JSON envelope — read
`error.code` first, `error.message` second, `meta.attempts` to see how far
the run got.

---

## A. Account suspend

A platform suspended/locked one account. Blast radius = 1 account; the risk
is burning the *next* account or proxy by routing around it carelessly.

### Signals

- `agent-reach farm account list` — `ban_status` flips from `new` to a
  lifecycle value (`banned` is what the bridge writes; the field is
  free-form, treat unknown values as opaque).
- XActions-side detection folds back automatically: `sync_bans()` maps a
  `locked` pool entry (dead-account codes 32/64/89/326 hard-lock, 353
  soft-lock) to `accounts.ban_status` + `xactions_link.ban_*` mirror.
- Envelopes: writes fail `UPSTREAM_BROKEN`/platform-verification errors
  naming the account; reads can still work — a suspended session is not a
  dead proxy, so the kill-switch stays silent.
- `agent-reach farm account ledger <name>` — a rising `attempts` count with
  zero `success` is the earliest smell, before the ban write-back lands.

### Contain

1. Stop dispatching to the account. There is no pause flag — remove it from
   whatever schedules `REACH_ACCOUNT=<name>` runs. The dispatch gates do the
   rest: nothing writes through an account you stop selecting.
2. Do **not** rotate its proxy or re-login-and-retry: a suspended account
   that keeps hammering burns the proxy's reputation with it.

### Diagnose

```sh
agent-reach farm account list                    # ban_status for every row
agent-reach farm account ledger <name>           # outcome mix over 24h
agent-reach farm account env <name>              # identity the runs resolved to

# pull the XActions pool verdict into the registry (idempotent):
uv run python -c "from agent_reach.runtime import xactions_bridge as b; \
  import json; print(json.dumps(b.sync_bans(), indent=1))"

# direct write-back when you already know the verdict:
uv run python -c "from agent_reach.runtime import xactions_bridge as b; \
  b.record_ban('<name>', 'banned', '<reason>')"
```

### Recover

Provision a replacement; do not resurrect credentials into the banned row.

```sh
agent-reach farm account add <new> --proxy <fresh-residential-url>
uv run python -c "from agent_reach.runtime import xactions_bridge as b; \
  b.link('<new>')"                                # → reach:<new> pool entry (locked placeholder)
uv run python -c "from agent_reach.runtime import xactions_bridge as b; \
  b.provision_session('<new>', '<cookie blob>')"  # unlocks the pool entry
```

- To reuse an existing pool entry instead: `b.link('<new>', '<pool-id>')`.
- `b.unlink('<name>')` drops only the mapping — the pool entry (and its
  cookies) is left for the operator to retire.
- Resume under normal guardrails: the new row's `age` is now, so the
  warm-up ramp (d0–3 → 25% cap) applies automatically — do not lift it.

### Prevent

Caps + ramp (contract §6), dispatch pacing, residential proxies 1:1 — the
chain-ban-by-behavior mitigation. The drift monitor (§C sibling doc
`farm-drift-monitor.md`) catches the platform-side cause that suspends
*every* account at once before it looks like a ban wave.

---

## B. Proxy pool down

One proxy endpoint — or the whole pool — is dead. Detection is instant:
the layer-1 kill-switch refuses every run before egress (`PLATFORM_BLOCKED`,
zero traffic, no ledger row).

### Signals

- Envelope: `{"error": {"code": "PLATFORM_BLOCKED", "message": "proxy dead
  for account <name>: <proxy> (ConnectionRefusedError|timeout …)"}}` with
  `meta.quota_refused: true` and empty `meta.attempts` — refused before any
  backend. A `(cached verdict …)` suffix means a probe within the last ~60s
  already failed.
- Pool-level outage = the same envelope across *several* accounts at once.
  Group `farm account list` rows by proxy host:port to size the blast radius.
- Registry-side failure is louder, not quieter: an unreadable registry
  emits `proxy kill-switch fail-closed … registry unreadable` — the
  kill-switch never defaults to allow.

### Contain

Already contained — the switch fails closed. The only wrong move is
bypassing it: `REACH_PROXY_CHECK=0` exists for a *verified-good* proxy the
probe can't reach (e.g. an endpoint that refuses idle TCP), never as an
incident shortcut.

### Diagnose

```sh
agent-reach farm account list                    # which rows share the dead proxy
agent-reach farm account ledger <name>           # attempts flat ⇒ nothing egressed
nc -z <proxy-host> <port>                        # the same TCP probe the switch runs
curl -x http://<proxy-host>:<port> -m 5 -sI https://example.com -o /dev/null -w '%{http_code}\n'
```

`nc` refused/timed-out ⇒ dead endpoint. `nc` succeeds but curl fails ⇒
endpoint alive, upstream/auth dead — still swap it. A hostname that does
not resolve at all is *uncheckable*: the probe is skipped (placeholder
proxies must not block), so DNS-level failures show up as backend errors,
not `PLATFORM_BLOCKED` — check the hostname string in the row first.

### Recover

```sh
# 1. Point the row at a live endpoint (no CLI verb — one UPDATE):
sqlite3 ~/.agent-reach/farm/registry.db \
  "UPDATE accounts SET proxy='http://<new-host>:<port>', updated_at=$(date +%s) \
   WHERE name='<name>';"
# (no sqlite3? uv run python -c "import sqlite3; …" — same statement)

# 2. Verify the new endpoint before the next run:
nc -z <new-host> <port>

# 3. Retry — the run must get *past* the kill-switch. Any other error code
#    (NEED_LOGIN, UPSTREAM_BROKEN, …) means traffic is flowing again.
REACH_ACCOUNT=<name> agent-reach twitter post "…"
```

Notes:

- The verdict cache (~60s, `REACH_PROXY_CHECK_TTL` to shorten) is
  process-local: a fresh CLI run re-probes, no manual flush needed.
- Legacy (non-farm) path uses `agent-reach configure proxy <url>` — the
  kill-switch does not cover it.
- Fallback-lane egress uses the *pool* entry's proxy column (copied at
  `link()`); a registry swap does not re-point it — re-`link()` or update
  `accounts.proxy` in `$XACTIONS_HOME/accounts.db` too.

### Prevent

Pool health is a fleet-manager concern (architecture item 9): poll
`agent-reach check` per container. Keep `REACH_PROXY_CHECK` at default;
the 60s dead-verdict cache is what stops a flapping endpoint from letting
every other run leak direct.

### Verified walkthrough (this host, 2026-11-22)

Ran end-to-end in an isolated `HOME` — full transcript in
`_bmad-output/…/story-incident-playbook-plan.md`. Account `wt1` on a dead
proxy (`http://127.0.0.1:1`):

1. `REACH_ACCOUNT=wt1 agent-reach twitter post …` → `PLATFORM_BLOCKED`
   *"proxy dead for account wt1 … ConnectionRefusedError"*, `quota_refused`,
   `attempts: []`.
2. `farm account ledger wt1` → `attempts: 0` — zero egress confirmed.
3. `nc -z` a stand-in live endpoint (`127.0.0.1:18080`), `UPDATE accounts
   SET proxy=…` — row swapped.
4. Retry → no `PLATFORM_BLOCKED`: the run reached the `opencli` backend and
   failed `UPSTREAM_BROKEN` (no session — a different incident class).
   Ledger now shows `post: attempts 1`. Traffic restored ⇒ playbook steps
   hit the documented state at every step.

---

## C. OpenCLI update broke the farm

An OpenCLI (or any pinned tool) upgrade ships a regression: write replays
break across accounts at once. Containment is the pin policy (C7):
**upgrade = rebuild**, never auto-update — so the incident is a canary
catching it, not a fleet discovering it.

### Signals

- Post-update envelopes: `UPSTREAM_BROKEN` on the `opencli` backend across
  *all* accounts — same action that worked before the update.
- `agent-reach doctor` flags `REGRESSED (worked at the previous check)`.
- Version mismatch: installed OpenCLI ≠ the tested pin
  (`@jackwener/opencli@2.0.3` — `runtime/tools.py` `tested` and the
  container Dockerfile; see `farm/container/README.md` pins table).

### Contain

Freeze the rollout: the pin already limits the blast radius to whatever
was rebuilt. Do not `agent-reach update` the remaining hosts/containers
until the canary clears.

### Diagnose

```sh
agent-reach doctor                              # REGRESSED marker on the channel
agent-reach check twitter                       # live failure on the canary
opencli --version 2>/dev/null || true           # vs tested pin in runtime/tools.py
docker compose -p farm-<acct> exec account opencli --version   # inside a container
docker image inspect agent-reach-farm:<tag> --format '{{json .Config.Labels}}'
```

### Recover — canary procedure

1. **Build the candidate on one container only.** Bump the OpenCLI pin in
   `farm/container/Dockerfile` (and `tested` in `runtime/tools.py` if the
   bump is intended to stick), then:
   ```sh
   docker build --platform linux/amd64 -f farm/container/Dockerfile \
     -t agent-reach-farm:<candidate-tag> \
     --build-arg AGENT_REACH_SOURCE_SHA="$(git rev-parse HEAD)" .
   ```
2. **Canary:** `docker compose -p farm-canary --env-file accounts/<one>.env up -d`
   — exactly one account container on the candidate image.
3. **Verify in-container:** `docker compose -p farm-canary exec account agent-reach check`
   returns a healthy JSON envelope; run one low-risk write (a reply to a
   test tweet beats a post).
4. **Rollout or rollback:**
   - Green → rebuild/retag the fleet image and `up -d` the rest.
   - Broken → `docker compose -p farm-canary down`, redeploy the previous
     tag (`agent-reach-farm:<last-good>`), revert the pin bump. The canary's
     own `~/.agent-reach` volume survives the downgrade — no session loss.
   - Host installs (non-container): `agent-reach update` reinstalls the
     private tool copy at its `tested` spec — pinning `tested` back is the
     host-side rollback.

### Prevent

The pin is the prevention: `tested` in `tools.py`, `@2.0.3` in the
Dockerfile, `AGENT_REACH_SOURCE_SHA` baked into the image — nothing
upgrades itself at runtime. Canary-1-then-fleet is the standing rule for
every version bump; `doctor`'s REGRESSED flag is the tripwire on hosts.

---

## Quick reference

| Signal in envelope | Incident | First command |
|---|---|---|
| `ban_status` ≠ `new` in `farm account list` | A — suspend | `farm account ledger <name>` |
| `PLATFORM_BLOCKED` + "proxy dead" | B — proxy | `nc -z <host> <port>` |
| `UPSTREAM_BROKEN` on `opencli`, all accounts, post-update | C — version | `agent-reach doctor` |
| `[doc-id drift]` alert line | rotation incoming | `agent-reach farm drift --once` (see `farm-drift-monitor.md`) |
