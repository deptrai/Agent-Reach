---
type: feature
ticket: '10'
route: full
risk: medium
status: built
title: Threads post write action
---

# Story 15.10 — Threads post write action

## Scope

`agent-reach threads post <text>` — text-only Threads posts via REST replay
(spike 15.9 verdict: REPLAYABLE, endpoint `POST /api/v1/media/configure_text_only_post/`).

## Auto Run Result

### Implementation

- `agent_reach/channels/_threads_write.py` — `_arm` (prepare + target + ready +
  `behavior.humanize("threads", tid)`), `_session` (`sessionid` + `ds_user_id`
  required → `NEED_LOGIN`), `_MINT_JS` (`getAsyncParams("POST")` + actor from
  `RelayAPIConfig.actorID`), `_SEND_JS` fire-and-poll with `_sent` dedup,
  `classify_threads` (checkpoint→`PLATFORM_BLOCKED`, 401/403→`NEED_LOGIN`,
  429→`RATE_LIMITED`, 404→`BAD_INPUT`, else→`UPSTREAM_BROKEN`).
- `agent_reach/channels/threads.py` — `ThreadsChannel(OpenCLISiteChannel)`:
  `post` action (Param `text`, ≤500 chars), write-only.
- `agent_reach/channels/__init__.py` — registered in `ALL_CHANNELS`.
- `agent_reach/runtime/dispatch.py` — `WRITE_ACTIONS["threads"] = {"post"}`.
- `tests/test_threads_write.py` — 8 tests (send path, empty/overlong,
  checkpoint/rate/login classification, confirmed-wins-over-errors,
  timeout→UPSTREAM_BROKEN, mint failure, channel wiring).
- Docs: `capabilities.md`, `SKILL.md`, `SKILL_en.md`, `README.md`,
  `docs/README_en.md`, `docs/multi-account-farm-architecture.md`,
  `docs/farm-account-contract.md`.

### Live verification (dedicated browser, account @epsiloncryptoai)

1. Real composer request captured via in-page fetch/XHR wrapper + CDP-driven
   composer (`Input.dispatchMouseEvent` + `Input.insertText`). Live params:
   `audience`, `container_module`, `creator_geo_gating_info`,
   `is_upload_type_override_allowed`, `music_failures_supported`, `nav_chain`,
   `publish_mode=text_post`, `should_include_permalink`, `text_post_app_info`
   (18-field JSON), `upload_id`, `web_session_id`, `jazoest` — ON TOP OF the
   `getAsyncParams` base set. **Missing params, not headers, caused the
   spike-assumed minimal body to 400** (`{"message":"","status":"fail"}`).
2. `web_session_id` accepts a fresh `__s` value verbatim (same
   `prefix:session:nonce` format) — no separate mint source needed.
   `x-bloks-version-id` is NOT required.
3. `agent-reach threads post "<real text>"` → `ok:true`, permalink
   `https://www.threads.com/@epsiloncryptoai/post/DgC55IBn11v`
   (media `4036042830355848559_56947282024`), 13.9s incl. behavior-sim pacing.
4. Probe garbage posts cleaned via composer UI (Polish locale:
   `Nowy wątek`/`Opublikuj`/`Usuń` — post options is the svg-only button inside
   `article`, not the nav "Więcej"). Only the dogfooding post remains.

### Deviations

- First `session_reuse.prepare("threads")` WIPED a manual session (copied
  anonymous cookies over live ones). Fix: after manual login, mark
  `session_reuse.threads = {"source": "manual"}` in state.json — `prepare`
  early-returns forever. Documented in memory + spike notes.
- Spike assumed `web_session_id` = `__s` (correct) but minimal param body
  (wrong) — body brought to the full captured set.
- `post-delete` descoped: needs `useBarcelonaDeletePostWWWMutation` doc-id
  (not yet captured) or UI flow.
- Threads UI is Polish-locale on this account — CDP selectors must not assume
  English labels.
- `cdp.call` opens a fresh WebSocket per call; event floods make large evals
  flaky on ShardBrowser — retry loops needed.

### Verification

- `uv run --extra dev python -m pytest tests/test_threads_write.py -q` → 8 passed.
- Full suite → 1124 passed.
- `agent-reach threads post` live → `ok:true`, permalink returned, post visible.
