---
title: 'Incident playbook + e2e walkthrough + sweep'
type: feature
ticket: '4'
created: '2026-11-22'
status: built
baseline_revision: '28cfb7c'
route: full
route_source: pinned
risk: low
review: quick
review_source: pinned
lenses_ran:
  - quick
context: []
warnings: []
deferred: []
---

## Intent

`docs/farm-incident-playbook.md`: three operator scenarios — (a) account
suspend (detect → quarantine → rotate/replace via the bridge), (b) proxy
pool down (detect → isolate → swap), (c) OpenCLI update broke (pin →
canary 1 container → rollback) — each with signals, containment, exact
diagnose commands, recovery, prevention. Walk through one scenario live on
this host (proxy-down), sweep docs for stale claims, keep the suite green.

## Code Map

- `docs/farm-incident-playbook.md` (new) — the 3 scenarios + quick-reference
  signal table. Links, does not duplicate, `farm-guardrails-playbook.md` /
  `farm-account-contract.md` / `farm-xactions-bridge.md` /
  `farm-drift-monitor.md` / `farm/container/README.md`.
- `docs/multi-account-farm-architecture.md` (sweep) — work-item table:
  mark 5/6/7/8/12/13/14 shipped with pointers; fix §1 stale "vault proxy
  field chưa wired" (wired since 16.1 `link()` + `undici.ProxyAgent`).
- No code changes. `cli.py` untouched (16.3's `farm drift` wiring already
  landed in HEAD via 16.2's commit).

## Tasks & Acceptance

- [x] Playbook exists with all 3 scenarios, each: signals → contain →
      diagnose (exact commands) → recover → prevent.
- [x] Walk-through of scenario B live on this host — every step's observed
      state matches the doc.
- [x] Docs sweep — stale "not implemented" claims in
      `multi-account-farm-architecture.md` fixed.
- [x] `uv run --extra dev python -m pytest tests -x -q` green.

### Verify

Playbook covers 3 scenarios; walkthrough reproduces the doc steps; suite
green.

## Auto Run Result

Walkthrough — scenario B (proxy pool down), isolated `HOME=/private/tmp/
farm-wt-tiDHBa` (registry + ledger under it; `/tmp` refused because macOS
`/tmp` is a symlink and `make_private_dir` rejects symlink paths — used
`/private/tmp`). Account `wt1`, dead proxy `http://127.0.0.1:1`.

```sh
$ HOME=$WT agent-reach farm account add wt1 --proxy http://127.0.0.1:1
{"ok": true, "action": "farm account add", "result": {"name": "wt1",
 "proxy": "http://127.0.0.1:1", "ban_status": "new", ...}}

# 1. Detect — the envelope IS the signal (playbook §B signals)
$ HOME=$WT REACH_ACCOUNT=wt1 agent-reach twitter post "walkthrough probe"
{"ok": false, "channel": "twitter", "action": "post",
 "meta": {"elapsed_ms": 5, "attempts": [], "quota_refused": true},
 "error": {"code": "PLATFORM_BLOCKED",
  "message": "proxy dead for account wt1: http://127.0.0.1:1
             (ConnectionRefusedError: [Errno 61] Connection refused)"}}

# 2. Confirm zero egress — ledger flat (playbook §B diagnose)
$ HOME=$WT agent-reach farm account ledger wt1
{"result": {"account": "wt1", "success": 0, "attempts": 0, "by_class": {}}}

# 3. Stand-in live endpoint + row swap (playbook §B recover steps 1–2)
$ python3 -c "…TCP listen on 127.0.0.1:18080…" &      # dummy "proxy"
$ nc -z 127.0.0.1 18080                              # → succeeded
$ sqlite3 $WT/.agent-reach/farm/registry.db \
    "UPDATE accounts SET proxy='http://127.0.0.1:18080', … WHERE name='wt1'"
wt1|http://127.0.0.1:18080

# 4. Retry — past the kill-switch, different failure class (step 3)
$ HOME=$WT REACH_ACCOUNT=wt1 agent-reach twitter post "walkthrough probe"
{"ok": false, "meta": {"elapsed_ms": 23595,
  "attempts": [{"backend": "opencli", "result": "UPSTREAM_BROKEN",
                "message": "Post could not be sent: txid_unavailable…"}]},
 "error": {"code": "UPSTREAM_BROKEN", …}}
#    → reached the real opencli backend; PLATFORM_BLOCKED gone = traffic path restored

# 5. Ledger now records the genuine attempt
$ HOME=$WT agent-reach farm account ledger wt1
{"result": {"attempts": 1, "by_class": {"post": {"success": 0, "attempts": 1}}}}
```

Every documented step landed: dead-proxy envelope (`PLATFORM_BLOCKED`,
`quota_refused`, empty `attempts`), flat ledger proving zero egress,
`nc -z` + sqlite `UPDATE` swap, retry passes the gate and fails at the
backend with a different class (`UPSTREAM_BROKEN` — no session in the
isolated HOME), ledger records the real attempt. Temp HOME and listener
removed after the run; real registry untouched.

Sweep: `docs/multi-account-farm-architecture.md` — items 5, 6, 7, 8, 12,
13, 14 marked shipped (caps, ramp, kill-switch L1, container template,
fallback, this playbook, drift monitor); §1 "vault proxy field chưa wired"
corrected to the shipped `link()`/`ProxyAgent` wiring. No other docs carry
stale farm claims (troubleshooting/install/README hits are generic).

Suite: `uv run --extra dev python -m pytest tests -x -q` → **1048 passed**
in 63s (final tree, docs-only story).

Deviations: registry proxy swap is a direct `sqlite3 UPDATE` — the CLI
intentionally has no `farm account update` verb (add/list/env/ledger
only); the playbook documents the UPDATE plus a `uv run python -c` variant.
Fallback-lane egress note added: a registry swap does not re-point the
XActions pool entry's proxy column — re-`link()` or update the pool row.
