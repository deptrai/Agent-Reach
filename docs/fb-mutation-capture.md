# Facebook mutation capture (write actions)

`facebook post` and the other write actions replay real `/api/graphql`
mutations with the **live transport envelope** harvested from the page via
`window.require("getAsyncParams")("POST")` — it carries `fb_dtsg`, `lsd`,
`jazoest`, `__spin_*`, `__dyn`, `__csr`, `__hs*` and friends, always
consistent with the current FB build. (An older design replayed a captured
`params` template; it broke on every deploy and was removed — see the
`getAsyncParams` comment in `agent_reach/channels/_facebook_share.py`.)

What still has to be captured once and committed is the `variables`
skeleton — including the full set of `__relay_internal__pv__*` provider
flags — as `agent_reach/channels/_fb_<name>_vars.json`.

## Capturing a new mutation

```bash
uv run python scripts/capture_fb_mutation.py \
    --profile <opencli-profile-with-facebook-login> \
    --name UFILikeMutation \
    --seconds 90
```

1. The script opens facebook.com in the profile's **headed** browser, arms an
   XHR/fetch interceptor, then polls for POST bodies to `/api/graphql`.
2. Do the action once by hand (click Like, post a comment, share…). Every
   captured `fb_api_req_friendly_name` is printed as it arrives.
3. Matching bodies are parsed; `variables` is **sanitized** and written to
   `agent_reach/channels/_fb_<name>_vars.json` (override with `--out`).

Sanitize rules (in `scripts/capture_fb_mutation.py:sanitize`): `actor_id` /
`__user` / `user_id` / `uid` → `"0"`, any UUID → zero-UUID
(`idempotence_token` keeps its `_FEED` suffix), epoch seconds/ms (12–13
digits starting with `1`) → `0`, `attribution_id_v2` → fixed template.
Everything else — including all `__relay_internal__pv__*` flags — is kept
verbatim; replay needs the full set or Facebook returns
`missing_required_variable_value` (error 1675012).

Never hand-edit a skeleton without re-verifying — run the action once after
capture to confirm.

## Re-capture after `doc_id` rotation

`doc_id` is a persisted-query id that **rotates with Facebook deploys**
(weekly-ish). Symptom: posts/likes start failing with `UPSTREAM_BROKEN` and
GraphQL error 1675012 / "Your Request Couldn't be Processed". Fix:

1. Re-run the capture script above — the new `doc_id` is printed.
2. Update `_DOC_ID` in the write module (e.g. `agent_reach/channels/_facebook_write.py`).
3. If the error persists, the `variables` shape changed too — re-capture the
   full skeleton and replace the `*_vars.json` file.
4. Re-run `pytest tests/test_facebook_write.py` plus one real post/like.

## Gotchas

- The interceptor lives in the page — any navigation to another origin kills
  it. Capture on the same view you act in (newsfeed for likes, post page for
  comments…). Re-arm by re-running the script.
- `--list-only` prints captured mutation names without writing files —
  useful to discover the right `fb_api_req_friendly_name` first.
- The dedicated (headless) profile can't be clicked by hand; use a headed
  profile for capture. The skeleton itself is account-agnostic after
  sanitization.
- **Never unescape `&quot;` across `innerHTML` to find embedded JSON** — user
  text contains `&quot;` too and unescaping it creates fake `"feedback_id":`
  matches (wrong-target writes). Match both raw and escaped forms in place:
  `/"feedback_id":"([^"]+)"|&quot;feedback_id&quot;:&quot;([^&]+)&quot;/g`
  (see `_LOCATE_JS` in `agent_reach/channels/_facebook_react.py`).
- **Verify results by parsed fields, not body substring** — Facebook JSON-
  escapes response text as `\uXXXX`, so `body.indexOf(postedText)` false-
  negatives on Vietnamese/emoji and makes a successful write look like a
  failure (duplicate on retry). Parse `data.*` fields or compare the
  unicode-escaped form (see `textEchoed` in `_facebook_post_edit.py`).

## Live e2e verification

When you verify a write action against a real account:

- The dedicated browser must hold a **live session** — check
  `agent-reach facebook profile <someone>` first. If actions start failing
  with `NEED_LOGIN` or empty page results, the session expired (FB rotates
  `c_user`/`xs`, sometimes invalidates them after checkpoint). Re-login via
  `agent-reach login facebook`, or inject fresh cookies from a logged-in
  browser into the dedicated profile via CDP `Network.setCookie`.
- Prefer `only_me` audience for test posts — and clean up test posts from
  the timeline afterwards (they accumulate).
- After an `UPSTREAM_BROKEN`, opencli may pause the channel for ~2 minutes
  (circuit breaker). Wait and retry — don't re-fire mutations blindly,
  some are idempotent but posts/comments are not (duplicates).
- Verify on the page itself, not just the action's exit code: permalink
  loads, reaction count flips, comment appears/disappears.
