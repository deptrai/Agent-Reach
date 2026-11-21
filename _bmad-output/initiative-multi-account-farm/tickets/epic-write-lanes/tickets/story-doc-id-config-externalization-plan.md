---
title: 'Doc-id config externalization'
type: feature
ticket: '2'
created: '2026-11-22'
status: built
baseline_revision: 58aa0a4ec9bd49caef1efae02eb64c7b1daec6be
route: full
route_source: default
risk: medium
review: none
review_source: default
lenses_ran: []
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** FB write lanes hardcode persisted-query `doc_id`s in five modules (`_facebook_write.py`, `_facebook_react.py`, `_facebook_comment.py`, `_facebook_post_edit.py`, `_facebook_share.py`); X queryIds join per spike 12.1. FB doc-ids rotate with deploys — a stale id is only fixable by a code edit + release today (covers R3 / farm-account-contract §4's PQD error class).

**Approach:** One hot-reloadable JSON config `<state.root()>/query-ids.json` (per-`state.root()` ⇒ per-account under farm `HOME` isolation) holding every persisted-query id in use, keyed `channel → name → id`. A minimal `agent_reach/runtime/query_ids.py` reads the file fresh per call (no module-level cache), resolves `get(channel, name, default)` with config-wins / module-constant-fallback semantics, and exposes `drift_probe(...)` that classifies a probe result as `global`/`per-account`/`single-account` (a single probe cannot distinguish global platform rotation from a per-account variant — that is the documented limitation). FB modules resolve their doc_id at send time through `get()`; `agent-reach doctor --probe-docids` (new opt-in flag — plain `doctor` stays no-network per its contract) probes one FB doc_id live and flags the channel stale on the "GraphQL document not found" signature with a re-capture hint.

## Boundaries & Constraints

**Always:** JSON + stdlib only; no module-level cache of config contents; config wins when the entry exists and is non-empty, module constant is the fallback (missing/empty/malformed config never breaks sends); keep `_DOC_ID`/`_DOC` names — `tests/test_facebook_*.py` read them as expected values; `pytest` baseline 879 green.

**Never:** No removal/rename of module-level constants tests rely on; no changes to `scripts/capture_fb_mutation.py` (owned by story 12.3); no fallback-lane routing (epic-farm-resilience); no proactive probing in plain `doctor` (no-network contract) — drift check is the opt-in `--probe-docids` flag; at most ONE live probe call total.

</intent-contract>

## Design

### Config file

`~/.agent-reach/query-ids.json` (via `state.root()` — follows `HOME`, so `farm/accounts/<name>/.agent-reach/query-ids.json` under a farm env window; each farm account can carry its own overrides):

```json
{
  "facebook": {
    "post_create":  "29221801510819514",
    "share":        "29304741702489334",
    "react":        "27646120298312844",
    "comment_create": "29145340888419828",
    "comment_delete": "28931450023144805",
    "post_edit":    "30241673288768290",
    "post_delete":  "29557927263819997"
  },
  "x": {}
}
```

A seeded copy ships at `agent_reach/channels/query-ids.json` documenting the current built-in ids — the runtime never reads it (fallback = module constants).

### `agent_reach/runtime/query_ids.py`

- `config_path() -> Path` → `state.root() / "query-ids.json"`.
- `get(channel, name, default="") -> str` — fresh read per call; config entry (non-empty string) wins, else `default`.
- `drift_verdict(results) -> "global"|"per-account"|"single-account"` — ≥2 probe results all-dead ⇒ `global`; mixed ⇒ `per-account`; exactly 1 ⇒ `single-account` (cannot decide global vs per-account — documented).
- `probe_facebook(eval_fn, target_id) -> dict` — evals a probe JS on a live facebook.com page: sends a POST to `/api/graphql/` with the resolved `post_create` doc_id and empty `variables`; returns `{ok, stale, status, error}` where `stale=True` iff the response carries "graphql document … not found" or HTTP 404.
- `drift_probe(channel, name, default, probe_fn) -> dict` — `{channel, name, doc_id, results, verdict}`; `probe_fn(doc_id) -> probe dict` injectable for tests.

### Module integration (send-time resolution)

- `_facebook_write.py`: `json.dumps(query_ids.get("facebook", "post_create", _DOC_ID))` in `facebook_post`.
- `_facebook_share.py`: `…get("facebook", "share", _DOC_ID)` in `_send_share`.
- `_facebook_react.py`: `…get("facebook", "react", _DOC_ID)` in `_send_reaction`.
- `_facebook_comment.py`: in `_send_mutation`, `doc_id = query_ids.get("facebook", f"comment_{kind}", doc_id)` after `_DOC[kind]` unpack.
- `_facebook_post_edit.py`: `f"post_{kind}"` same pattern.

### Doctor integration

`manage.doctor(probe_doc_ids=False)` gains result key `doc_id_drift` (`None` when not probed); `cli.py` adds `--probe-docids`. Probe path: lazily import `_facebook_write`, reuse `fb_write_target`-style scaffold → `query_ids.probe_facebook`. `stale → True` ⇒ `{"channel": "facebook", "stale": true, "hint": "re-capture skeleton / update doc-id config (~/.agent-reach/query-ids.json)"}`.

## Tasks & Acceptance

**Execution:**
- [ ] `agent_reach/runtime/query_ids.py` — loader + verdict + probe (stdlib only).
- [ ] 5 FB modules — send-time doc_id resolution through `query_ids.get`.
- [ ] `agent_reach/channels/query-ids.json` — seeded defaults.
- [ ] `manage.doctor` + `cli._cmd_doctor` — `--probe-docids` flag + `doc_id_drift` result.
- [ ] `tests/test_query_ids.py` — override resolution, hot reload (rewrite config between calls, no re-import), malformed/missing config fallback, verdict logic (global/per-account/single-account), doctor drift verdict via stubbed probe.
- [ ] Plan-file status → built; `tickets.py mark epic-write-lanes 2 built`; commit on `story-12.2-docid-config`.

**Acceptance Criteria:**
- Given `query-ids.json` maps `facebook.post_create` to `"999"`, when `facebook_post` builds its send expr, then `doc_id` is `"999"` — editing the file again changes the next send without re-import.
- Given a missing/empty/malformed config, when a send runs, then module constants apply unchanged.
- Given a probe returning "graphql document not found", when `doctor --probe-docids` runs, then `doc_id_drift.stale == true` with the re-capture hint; ≥2 probe results all-dead ⇒ `global`, mixed ⇒ `per-account`, one result ⇒ `single-account`.

## Verification

```bash
uv run --extra dev python -m pytest tests -x -q
```

## Auto Run Result

- `agent_reach/runtime/query_ids.py` (new): `config_path()` → `state.root()/query-ids.json`; `load()`/`get()` fresh-read per call (no cache — hot reload); `dead_document()` detects the persisted-query-dead signature (HTTP 404 or "graphql document … not found"); `drift_verdict()` → `global` (≥2 all stale) / `per-account` (mixed) / `live` / `single-account`; `drift_probe()` resolves the id then runs an injectable `probe_fn`; `probe_facebook()` fires one in-page `/api/graphql/` POST with the resolved id + empty `variables`.
- `agent_reach/channels/_facebook_write.py`, `_facebook_react.py`, `_facebook_share.py`: `__FB_DOC` now binds `query_ids.get("facebook", "<name>", _DOC_ID)` at send time; `_DOC_ID` kept as fallback.
- `agent_reach/channels/_facebook_comment.py`, `_facebook_post_edit.py`: `doc_id = query_ids.get("facebook", f"comment_{kind}"/f"post_{kind}", doc_id)` after `_DOC[kind]` unpack; `_DOC` kept as fallback (tests still read it).
- `agent_reach/channels/query-ids.json` (new): seeded copy of all 7 baked-in ids + empty `x` section — documentation for operators; runtime never reads it.
- `agent_reach/runtime/manage.py`: `doctor(probe_doc_ids=False)` + `_doc_id_drift()` — result key `doc_id_drift` (`None` unless probed); probe failure → `{"verdict": "unprobed"}`, never breaks doctor.
- `agent_reach/runtime/cli.py`: `doctor --probe-docids` flag (JSON + `--text` rendering); HELP updated.
- `tests/test_query_ids.py` (new): 14 tests — resolution/fallback/hot-reload/malformed config, send-time expr binding, `dead_document` signature, `drift_verdict` global/per-account/single-account, `drift_probe`, `probe_facebook` expr, doctor stale/fresh/unprobed/offline paths.
- `pytest tests -x -q`: **893 passed** (baseline 879 + 14).
- LIVE verify (1 probe, real FB GraphQL via dedicated browser): `agent-reach doctor --probe-docids` → `{status: 200, body: "missing_required_variable_value…"}` — live doc fails validation as designed ⇒ `stale: false`, `verdict: "single-account"`, `hint: ""`. Exactly one probe fired.
- Deviations: drift probe wired behind opt-in `--probe-docids` flag (plain `doctor` is contractually no-network). Per-account probing of ≥2 sessions in one invocation descoped — `drift_verdict` implements the correlation; `single-account` documents what one probe cannot decide.
