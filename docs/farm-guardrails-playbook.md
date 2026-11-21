# Farm guardrails playbook — e2e for operators

How to drive and read the dispatch guardrails (contract §6, stories 13.1–13.3)
against a real account row. Every command prints exactly one JSON envelope;
the refusals below are the whole reason the guardrails exist — learn to read
them before scaling writes.

## 1. Register an account

```sh
agent-reach farm account add test --proxy http://127.0.0.1:8080
```

`farm account env test` shows the identity a run resolves to (`HOME`,
`REACH_BROWSER_PROXY`, `REACH_ACCOUNT`). The row stores the onboard epoch in
`accounts.age` — the warm-up ramp reads it.

## 2. Set a cap and run the write lane

Caps are per action-class per rolling 24h of `success` rows. A class with no
default (`DEFAULT_CAPS`) is unlimited until you set `REACH_CAP_<CLASS>`.

```sh
REACH_ACCOUNT=test REACH_CAP_POST=2 agent-reach twitter post "first"
REACH_ACCOUNT=test REACH_CAP_POST=2 agent-reach twitter post "second"
REACH_ACCOUNT=test REACH_CAP_POST=2 agent-reach twitter post "third"   # refused
```

The third post returns `ok=false` with:

```json
{"error": {"code": "QUOTA_EXCEEDED",
           "message": "quota cap exceeded for account test action_class post: 2 success in 24h (cap 2)"}}
```

`meta.quota_refused` is set on every gate refusal — it never reached the
platform, so `agent-reach farm account ledger test` still shows `success: 2`.
`REACH_CAP_POST=0` refuses every post (freeze switch, not a ledger problem).

## 3. Dead proxy → PLATFORM_BLOCKED, zero egress

Kill the proxy (or point the row at a dead port, e.g. `http://127.0.0.1:1`) and
any action — reads included — refuses before a backend or the quota gate:

```json
{"error": {"code": "PLATFORM_BLOCKED",
           "message": "proxy dead for account test: http://127.0.0.1:1 (...)"}}
```

The kill-switch is a TCP handshake to the registry proxy (5s), verdict cached
~60s. Diagnosis order:

1. `agent-reach farm account ledger test` — confirm no new `attempts` rows: a
   refusal never reached a platform.
2. Check the proxy itself (`nc -z host port`); a refused handshake is a dead
   proxy, an unresolvable hostname is uncheckable and skipped.
3. `REACH_PROXY_CHECK=0` bypasses the check for a verified-good proxy;
   `REACH_PROXY_CHECK_TTL=<sec>` shortens the dead-verdict cache after a fix.

## 4. Warm-up ramp — caps tighten by account age

`accounts.age` (onboard epoch) scales the resolved cap before the ledger
compare: **d0–3 → 25%, d4–7 → 50%, d8–14 → 75%, d15+ → 100%** (`RAMP_SCHEDULE`).
Effective cap = `max(1, floor(cap × factor))`; a fresh account with
`REACH_CAP_POST=8` therefore posts only 2/day at first. A ramped refusal names
the scale: `... 2 success in 24h (cap 8 x ramp 0.25 (account age 1d) = 2)`.

- Missing/unreadable `age` → mature (factor 1.0); cap `0` stays refuse-all.
- `REACH_RAMP="days:factor,..."` retunes the schedule, e.g. a conservative
  `"7:0.1,30:0.5"`. An invalid value → `BAD_INPUT` at dispatch.

## 5. e2e reference

`tests/test_farm_guardrails_e2e.py` chains all of the above on the X lane in
isolated HOMEs: over-cap refuse, dead-proxy `PLATFORM_BLOCKED`, day-1 ramp cut,
ramp→kill-switch ordering, and the CLI JSON envelope end to end.
