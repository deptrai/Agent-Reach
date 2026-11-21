---
title: 'Farm account contract doc'
type: feature
ticket: '2'
created: '2026-11-21'
status: built
baseline_revision: a38d68eb82b438eb0c56e628a6e9a0744897f916
route: oneshot
route_source: pinned
risk: low
review: none
review_source: pinned
lenses_ran: []
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** The farm epics that follow (write-lanes, guardrails, fleet-orchestration, resilience) each assume per-account identity, kill-switch, ledger and error-routing semantics that the architecture doc only implies. Without a single binding contract each epic invents its own — the exact failure the substrate epic exists to prevent.

**Approach:** Write `docs/farm-account-contract.md` (R3): the one authoritative definition of identity-per-run, two-layer kill-switch, rolling-24h ledger + multi-writer concurrency, `PERSISTED_QUERY_DEAD` + fallback route rule, and the registry schema — with an explicit contract-vs-deferred split so later stories know exactly what they implement versus what is already binding.

## Boundaries & Constraints

**Always:** Cover exactly R3 + ticket checklist: (a) identity `REACH_ACCOUNT` → registry → env; unknown → `NEED_SETUP`, never silent default `HOME`. (b) Kill-switch semantics "zero egress outside proxy_i — fail-closed" (NOT "verify exit IP = proxy" — residential proxies rotate): layer 1 dispatch soft-check = best-effort pre-flight; layer 2 netns hard-enforce = deferred to epic-fleet-orchestration. (c) Ledger rolling-24h + multi-writer: WAL; only `success` counts toward quota, attempts are a separate counter; fallback-lane writes must also be recorded. (d) Error class `PERSISTED_QUERY_DEAD` + route-to-fallback rule. (e) Schema doc.

**Never:** No code changes (doc-only). Do not define enforcement mechanisms beyond semantics (caps/warm-up = epic-account-guardrails; netns/container = epic-fleet-orchestration; fallback routing = epic-farm-resilience). Do not contradict implemented facts in `agent_reach/runtime/farm.py` (story 11.1).

</intent-contract>

## Tasks & Acceptance

**Execution:**
- [ ] `docs/farm-account-contract.md` — write contract doc per intent above.

**Acceptance Criteria:**
- Given the ticket checklist, when the doc is read, then all 5 contract sections exist (identity, kill-switch, ledger, error class, schema) plus a contract-vs-deferred split.
- Given `agent_reach/runtime/farm.py`, when the schema section is compared, then it matches the implemented `accounts` table exactly.

## Implementation Notes

- Precedent for the honest deferred-work style: `docs/fb-messenger.md` (descope note).
- Sources of truth: epic R3 + Done-when (epic-account-substrate.md), initiative decisions (fallback egress via account proxy, kill-switch semantics, PQD route), `farm.py` accounts schema, `result.py` error codes/envelope, `docs/multi-account-farm-architecture.md` §3/§5.
- Env contract mirrors story 11.3's planned shape: `{HOME, REACH_BROWSER_PROXY, REACH_ACCOUNT}`; unset `REACH_ACCOUNT` = legacy single-account path unchanged; set-but-unknown = `NEED_SETUP`.
- `actions_log` is contract-defined here (implemented story 11.5): `(account, ts, action_class, outcome)`; outcome vocabulary = `success` | result.py error code.

## Verification

```bash
ls docs/farm-account-contract.md
grep -c "NEED_SETUP\|fail-closed\|rolling\|PERSISTED_QUERY_DEAD\|CREATE TABLE" docs/farm-account-contract.md
```

Doc-only: no pytest.

## Auto Run Result

**Outcome:** `docs/farm-account-contract.md` written — 6 sections covering the ticket checklist (identity per-run, kill-switch 2 layers, ledger rolling-24h + multi-writer, `PERSISTED_QUERY_DEAD` + route rule, schema) plus a contract-vs-deferred table binding each item to its owning epic/story.

**Delivered:** contract records — `REACH_ACCOUNT` → registry → `{HOME, REACH_BROWSER_PROXY, REACH_ACCOUNT}`, unknown → `NEED_SETUP` never silent default `HOME`, unset = legacy single-account path unchanged; kill-switch = "zero egress outside proxy_i, fail-closed" (explicitly NOT exit-IP equality, residential proxies rotate), layer-1 dispatch soft-check best-effort now / layer-2 netns deferred to epic-fleet-orchestration; ledger `actions_log` counts only `success` toward quota, attempts a separate counter, fallback-lane writes must record, WAL multi-writer rules (short txns, busy_timeout ≥5s, bounded BUSY retry); `PERSISTED_QUERY_DEAD` final-per-lane + route-to-fallback rule (routing impl deferred to epic-farm-resilience, fallback egress must use account proxy); schema documents implemented `accounts` table verbatim from `farm.py` + contract-defined `actions_log`.

**Verification:** doc exists, all 5 checklist sections + contract-vs-deferred split present; `accounts` DDL matches `farm.py` `_SCHEMA` exactly. Doc-only — no pytest run.

**Residual:** none. Follow-on implementers: 11.3 env shape, 11.4 dispatch resolve, 11.5 ledger, 11.6 error class — all consume this contract.
