---
title: 'Kill-switch soft-check ở dispatch'
type: feature
ticket: '2'
created: '2026-11-22'
status: built
baseline_revision: '56af71e'
route: full
route_source: pinned
risk: medium
review: quick
review_source: pinned
lenses_ran:
  - quick
followup_review_recommended: false
context: []
warnings: []
deferred:
  - summary: >-
      Probe is TCP-handshake only — it proves the proxy socket accepts a
      connection, not that the proxy relays traffic. A live-but-broken relay
      (e.g. auth rejection) passes the check.
    evidence: |-
      `_proxy_alive` calls `socket.create_connection` and nothing else; the
      ticket's optional HEAD/CONNECT step needs a target host, which is itself
      egress and false-fails a healthy proxy that blocks that one URL.
      Commented in code as `shortcut:`.
    location: agent_reach/runtime/dispatch.py:_proxy_alive
    severity: low
  - summary: >-
      Proxy hosts that do not resolve (unresolvable DNS) are treated as
      uncheckable and skip enforcement — a misconfigured account proxy of that
      shape gets no kill-switch protection.
    evidence: |-
      `_proxy_alive` maps `socket.gaierror` to `_ProxyUncheckable` and the gate
      returns without a verdict. Chosen so placeholder proxies (`http://p1:8080`
      in the existing suite, accounts not yet wired) don't fail-close every
      run; a genuinely dead-but-resolvable proxy still refuses fail-closed.
      The exception class exists solely so a later story can tighten this.
    location: agent_reach/runtime/dispatch.py:_enforce_proxy_kill_switch
    severity: low
  - summary: >-
      Verdict cache is process-local, so the ~60s dedup only helps bursts
      inside one CLI process; separate invocations re-probe every time.
    evidence: |-
      `_PROXY_VERDICTS` is a module dict. The ticket allows this explicitly
      ("module dict is fine, single-process CLI"); a farm-runner daemon
      re-checking per spawned process is the accepted cost.
    location: agent_reach/runtime/dispatch.py:_PROXY_VERDICTS
    severity: low
---

## Intent

Under `REACH_ACCOUNT`, dispatch runs a best-effort proxy liveness probe **before any backend** for every action — reads included (contract §2 layer-1: zero egress outside proxy_i; a read through a dead-proxy account egresses direct = IP leak). Dead/unconfirmed proxy → `PLATFORM_BLOCKED` refusal, no direct fallback, no IP comparison. Skips when the account has no proxy (proxy-less = valid direct per contract) or `REACH_ACCOUNT` is unset (legacy path). Verdicts cache ~60s per proxy URL so bursts pay one probe.

## Code Map

- `agent_reach/runtime/dispatch.py` — `_run_action` enters the env window (`_farm_env_enter`) then runs the quota gate. The kill-switch goes **first** inside that same window, before `_enforce_write_quota`: counting quota on a dead egress is moot, and the ordering pins PLATFORM_BLOCKED as the surfaced refusal. `meta.quota_refused` (13.1) already makes the record seam skip gate refusals — reused unchanged: a proxy refusal never reached the platform, so no ledger row.
- `agent_reach/runtime/farm.py` — `accounts.proxy` is the authoritative proxy source (contract §5). New `account_proxy(name)` returns the stored proxy or `None` (empty string → proxy-less), mirroring `account_env`'s error shape (`NEED_SETUP`/`BAD_INPUT`). Env var is only a mirror — the registry wins.
- `agent_reach/runtime/result.py` — `PLATFORM_BLOCKED` already exists and is in `FINAL_CODES`; no change.
- `docs/farm-account-contract.md` §2 — layer-1 semantics this implements; layer-2 netns is epic-fleet-orchestration, untouched.
- Tests: `tests/conftest.py` isolates `HOME` per test; `127.0.0.1:1` refuses a connect instantly.

## Tasks & Acceptance

- [x] `farm.py`: `account_proxy(name)` — registry-authoritative proxy lookup.
- [x] `dispatch.py`: `_proxy_alive` (TCP connect, 5s timeout), `_PROXY_VERDICTS` cache, `_proxy_check_ttl` (`REACH_PROXY_CHECK_TTL`), `_enforce_proxy_kill_switch` wired into `_run_action` ahead of `_enforce_write_quota`.
- [x] Escape hatch `REACH_PROXY_CHECK=0|off|false|no`.
- [x] Refusal → `PLATFORM_BLOCKED` naming account + proxy + reason; envelope flagged `meta.quota_refused` → no ledger row.
- [x] `tests/test_farm_killswitch.py` — 12 tests.
- [x] `uv run --extra dev python -m pytest tests -x -q` green.

### Verify

`REACH_ACCOUNT=a1` + proxy `http://127.0.0.1:1` → `ok:false`, `error.code == PLATFORM_BLOCKED`, backend call count 0; proxy-less account → `ok:true`.

## Implementation Notes

- **Scope decision: all actions, not just writes.** Contract §2 applies zero-egress to reads too ("reads leak the IP just the same"). A read refused by a dead proxy is the correct, documented-by-contract behavior; the ticket's "trước khi chạy action" carries no write qualifier.
- **Ordering:** kill-switch before quota — no point counting quota when egress is dead, and PLATFORM_BLOCKED must win over QUOTA_EXCEEDED as the surfaced code (test pins this).
- **Check method:** TCP handshake only (`socket.create_connection`, 5s). The ticket's HEAD/CONNECT alternative needs a real target URL, which is itself egress through the proxy and false-fails a proxy that is up but blocks that target. Handshake is the minimum viable liveness signal; marked `shortcut:` in code.
- **Check-failure policy:** fail-closed to `PLATFORM_BLOCKED` — contract §2: "when proxy_i's usability cannot be confirmed the run does not proceed". Any probe error (refused, timeout, malformed URL) means unconfirmed. One carve-out: `socket.gaierror` (unresolvable host) → `_ProxyUncheckable` → skip, so placeholder proxies don't refuse every run; deferred finding above records the tightening path.
- **Cache:** module dict `proxy → (ok, expires_at)`, monotonic clock, TTL 60s default / `REACH_PROXY_CHECK_TTL` seconds (0 = always re-probe). Both OK and FAIL verdicts cache. Process-local per the ticket's allowance.
- **`meta.quota_refused` reuse:** the flag's meaning is "gate refused pre-platform, don't ledger". Renaming it `gate_refused` would touch 13.1's tests for cosmetic gain — kept as-is; the comment at the call site now covers both gates.
- **No `Optional` import change to dispatch:** already imported `Optional` from typing; farm.py gained `from typing import Optional`.

## Plan Change Log

- 2026-11-22: created (route pinned `full`, risk medium from the ticket).
- 2026-11-22: scope pinned to all actions per contract §2; DNS-unresolvable carve-out added after `test_registered_account_runs_under_account_home` failed on placeholder proxy `http://p1:8080` — fail-closed on resolvable-but-dead stays intact.

## Verification

```bash
uv run --extra dev python -m pytest tests/test_farm_killswitch.py -q --tb=short
uv run --extra dev python -m pytest tests -x -q --tb=short
```

## Auto Run Result

**Summary** — Every action under `REACH_ACCOUNT` (reads included, per contract §2 zero-egress) is pre-flighted against the account's **registry** proxy before any backend: TCP handshake, 5s timeout, verdicts cached ~60s per URL. Dead or unconfirmed → `PLATFORM_BLOCKED` naming account + proxy + reason, flagged `meta.quota_refused` so no ledger row is written. Proxy-less rows, the legacy path, and `REACH_PROXY_CHECK=0` skip the gate; `REACH_PROXY_CHECK_TTL` tunes the cache (0 = re-probe every run). The check runs ahead of the quota gate — PLATFORM_BLOCKED surfaces, never masked as a quota refusal.

**Check method** — TCP `create_connection` handshake only. HEAD/CONNECT-through-proxy was rejected: it needs a real target (itself egress) and false-fails a healthy proxy that blocks that target.

**Scope** — All actions, not just writes: contract §2 states a dead-proxy read egresses direct and leaks the IP. Chosen and tested.

**Files changed**
- `agent_reach/runtime/dispatch.py` — `_ProxyUncheckable`, `_proxy_alive`, `_PROXY_VERDICTS`, `_proxy_check_ttl`, `_enforce_proxy_kill_switch`, gate call inside the env window before `_enforce_write_quota`; `socket`/`urlparse` imports, `PLATFORM_BLOCKED` import.
- `agent_reach/runtime/farm.py` — `account_proxy(name)` registry lookup; `Optional` import.
- `tests/test_farm_killswitch.py` — 12 tests: dead proxy refuse (read + write), proxy-less pass, unset-account pass (+ hostile stale env), live-proxy pass + cached OK, cached FAIL within ttl, ttl=0 re-probe, check-off bypass, invalid ttl BAD_INPUT, registry-failure fail-closed, unresolvable host uncheckable, kill-switch-before-quota ordering, no-ledger on refusal.

**Review findings** — 3 `low`, all deferred: handshake-only probe (can't prove relay), unresolvable-host carve-out (placeholder proxies get no protection), process-local cache (cross-process re-probes). None patched — each is a documented contract/spec trade-off.

**Verification performed** — `pytest tests/test_farm_killswitch.py` → 12 passed; farm group (dispatch/e2e/quota/ledger/killswitch) → 44 passed; full suite `pytest tests -x -q --tb=short` → 998 passed, 0 failed.

**Residual risks**
- Existing farm tests rely on `p1`/`p2` staying unresolvable; a resolver that answers single-label names would flip them to `PLATFORM_BLOCKED`. New tests use `.invalid` (RFC 2606) to avoid depending on that.
- Accounts storing a proxy whose host does not resolve get no kill-switch until the carve-out is tightened.
