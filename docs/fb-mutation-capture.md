# Facebook mutation capture (write actions)

`facebook post` and future write actions replay real `/api/graphql` mutations
with tokens harvested live from the page (`fb_dtsg`, `lsd`, `jazoest`,
`__spin_*`). The non-secret part of the request — the `variables` skeleton
including `__relay_internal__pv__*` provider flags — is captured once and
committed as `agent_reach/channels/_fb_<name>_vars.json`.

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
