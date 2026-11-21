---
title: 'Generalize capture pipeline'
type: feature
ticket: '3'
created: '2026-11-22'
status: built
baseline_revision: '58aa0a4ec9bd49caef1efae02eb64c7b1daec6be'
route: full
route_source: auto
risk: medium
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** `scripts/capture_fb_mutation.py` only captures Facebook `/api/graphql` POSTs keyed by `fb_api_req_friendly_name`. Epic write-lanes needs the same record → sanitize → skeleton pipeline for X, whose requests are `POST/GET https://x.com/i/api/graphql/<queryId>/<Operation>` with auth carried in headers (`authorization` Bearer, `x-csrf-token`, `x-client-transaction-id`, `x-twitter-*`) — spike 12.1 proved all tokens are reachable from page context.

**Approach:** One script `scripts/capture_mutation.py --platform fb|x`. FB path keeps the existing opencli driver, arm JS, `parse_body`, `sanitize`, `default_out_name` and `{params, variables}` output byte-compatible. X path drives the dedicated browser over CDP (`Target.createTarget` + `Runtime.evaluate`), intercepts fetch + XHR recording `{op, url, queryId, method, headers, body}` for `/i/api/graphql/`, sanitizes with shared rules + X secrets, writes `{platform, operation, method, query_id, headers_template, body_template}` skeletons keyed by operation. `scripts/capture_fb_mutation.py` becomes a thin re-export shim so `capture_fb_comment.py`/`capture_fb_post_edit.py`/`capture_fb_share.py` (which `from capture_fb_mutation import parse_body, sanitize`) keep working unmodified.

## Boundaries & Constraints

**Always:** Reuse `agent_reach.runtime.cdp` + `browser.ensure_running` for X (spike: `Page.addScriptToEvaluateOnNewDocument` doesn't run in main world on CloakBrowser — arm via `Runtime.evaluate` on the loaded page). Own tab only (`Target.createTarget`). Shared `sanitize()` stays byte-identical for FB (X-only id keys passed as an extra set, never merged into `_ID_KEYS` defaults). NEVER persist `auth_token`/`ct0`/bearer/`x-client-transaction-id`/cookie values — placeholders only. No real tweet/like posted during verify — read-side op only.

**Never:** No edits to `agent_reach/runtime/query_ids.py` (story 12.2 boundary), no `agent_reach/channels/_x_*` channel modules (story 12.4), no edits to the three `capture_fb_*` wrappers (shim keeps them compatible), no DOM-click automation for capture.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_FB | `--platform fb --profile P --name X` | same skeleton as today (`_fb_<slug>_vars.json`) | same as today |
| HAPPY_X | `--platform x --name HomeTimeline`, dedicated browser logged in | `_x_<op>_vars.json` skeleton, secrets scrubbed | — |
| X_NO_SESSION | X tab opens but logged out | still captures guest/graphql traffic; skeleton headers show missing auth | note in output |
| NOTHING_CAPTURED | watch window ends empty | exit 1 + "nothing captured" hint | stderr |
| BODY_NOT_JSON | X GET op (variables in query string) | body_template built from query params instead | — |
| SHIM | wrappers import `capture_fb_mutation` | `parse_body`/`sanitize`/`default_out_name`/`_ZERO_UUID`/`_UUID_RE` all importable | — |

</intent-contract>

## Code Map

- `scripts/capture_mutation.py` — NEW, owns everything: shared `sanitize`/`parse_body`/`default_out_name`/`oc`/`oc_eval` (moved verbatim from capture_fb_mutation.py), `--platform` argparse switch, X arm JS (fetch+XHR, headers capture), X body parse (JSON POST + query-string GET), `sanitize_x_headers`, `default_out_name_x`, CDP tab helpers (`_x_target`, `_cdp_eval`).
- `scripts/capture_fb_mutation.py` — rewritten as shim: adds its dir to `sys.path`, re-exports the FB surface, `main()` delegates with `--platform fb` default. Existing CLI invocations keep working.
- `docs/fb-mutation-capture.md` — keep filename (referenced by `docs/multi-account-farm-architecture.md`), retitle to cover FB + X: X capture recipe, token provenance table from spike 12.1, plus existing `&quot;` / `\uXXXX` / `getAsyncParams` lessons.
- `tests/test_x_capture.py` — NEW: X sanitize (synthetic CreateTweet body), header scrubbing, op/queryId URL extraction, GET-vs-POST body parse, default out name, shim re-export surface.
- `tests/test_fb_capture.py` — unchanged (loads the shim by path; proves byte-compat).

## Tasks & Acceptance

**Execution:**
- [x] `scripts/capture_mutation.py` — generalized script (FB path moved verbatim, X path via CDP)
- [x] `scripts/capture_fb_mutation.py` — compat shim re-exporting `parse_body`, `sanitize`, `default_out_name`, `oc`, `oc_eval`, `_ZERO_UUID`, `_UUID_RE`, `_EPOCH_RE`, `_ID_KEYS`, `main`
- [x] `docs/fb-mutation-capture.md` — dual-platform doc
- [x] `tests/test_x_capture.py` — sanitize/routing/shape tests on synthetic X captures
- [x] live verify — one real non-mutating X graphql capture on the dedicated browser (fallback: synthetic if X session down)

**Acceptance Criteria:**
- Given `--platform fb`, when run with the documented flags, then output shape `{params, variables}` is identical to today
- Given a logged-in X tab, when `--platform x` runs, then a skeleton keyed by operation name is produced with `headers_template` placeholders and no secret values on disk
- Given the three `capture_fb_*` wrappers, when they `from capture_fb_mutation import parse_body, sanitize`, then the import succeeds unchanged
- `uv run --extra dev python -m pytest tests -x -q` stays green (baseline 879)

## Implementation Notes

## Plan Change Log

## Review Triage Log

## Design Notes

X body shape differs from FB: JSON `{variables, features, queryId}` instead of form-urlencoded `variables=...` param. Skeleton = `{platform, operation, method, query_id, path_template, headers_template, body_template}`; `body_template` always holds `{variables, features, queryId}` regardless of GET/POST (GET replays them as query params). Header placeholders: `authorization` → `Bearer <bearer>` (live via `__wr('973526').Rb()`), `x-csrf-token` → `<ct0>` (live from `document.cookie`), `x-client-transaction-id` → `<fresh-txid>` (live via `__wr('265390').kc`), `cookie`/`set-cookie` dropped, `x-twitter-*` constants kept verbatim.

## Verification

**Commands:**
- `uv run --extra dev python -m pytest tests -x -q` — all pass (baseline: 879)
- `uv run python scripts/capture_mutation.py --platform x --name HomeTimeline --seconds 60 --list-only` — lists captured ops on dedicated browser
- `uv run python scripts/capture_mutation.py --platform x --name <op> --seconds 60` — writes `agent_reach/channels/_x_<op>_vars.json`

## Auto Run Result

- **Summary**: `scripts/capture_mutation.py --platform fb|x` — FB path moved verbatim (opencli driver, `__fbCap`, `{params, variables}` output, byte-compat shim `capture_fb_mutation.py` re-exporting `parse_body`/`sanitize`/`default_out_name`/`oc`/`oc_eval`/`_UUID_RE`/`_ZERO_UUID`/`_EPOCH_RE`/`_ID_KEYS`/`main` for the three `capture_fb_*` wrappers); X path arms `window.__cap` via CDP `Runtime.evaluate` on a dedicated-browser x.com tab, records `{op, url, queryId, method, headers, body}` for `/i/api/graphql/` fetch+XHR GET+POST, writes `{platform, operation, method, query_id, path_template, headers_template, body_template}` skeletons to `_x_<op>_vars.json`.
- **Files**: `scripts/capture_mutation.py` (new, 430 lines); `scripts/capture_fb_mutation.py` (rewritten as 30-line shim); `docs/fb-mutation-capture.md` (dual-platform doc, filename kept — referenced by `docs/multi-account-farm-architecture.md`); `tests/test_x_capture.py` (18 tests); `tests/test_fb_capture.py` (unchanged).
- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **897 pass** (879 baseline + 18 new). Live X capture on dedicated browser (logged-in x.com session, compose button + ct0 present): armed via Runtime.evaluate, scrolled timeline → 14 requests captured, `POST HomeTimeline` (query_id `KmJCeZ__ef1fNos7tKN_fg` — matches spike 12.1) → `/tmp/_x_home_timeline_vars.json`. Inspected: `headers_template` has `Bearer <bearer>` / `<ct0>` / `<fresh-txid>` placeholders, `x-twitter-*` statics kept, no cookie; `body_template` keeps `{variables, features, fieldToggles, queryId}`; zero real secrets in file.
- **Deviations**: captured skeleton kept at `/tmp/` as evidence, NOT committed to `agent_reach/channels/` — `_x_*` channel files are story 12.4's boundary; 12.4 re-captures per-action skeletons via the same command. `--url` flag semantics are platform-scoped (fb: open target / x: new-tab URL).
- **Residual risks**: X op names must be filtered by `--name` or every op in the window is written; full page reload (non-SPA) kills the interceptor — re-arm needed; `x-twitter-*` whitelist assumes those headers stay credential-free.
