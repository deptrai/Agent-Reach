# Mutation capture (write actions) — Facebook + X

Write actions replay real GraphQL requests captured once from a logged-in
page, then re-issued with **live tokens** — never replayed blobs. The
capture pipeline is one script:

    scripts/capture_mutation.py --platform fb|x

`scripts/capture_fb_mutation.py` is a back-compat shim (same CLI flags,
same output, re-exports `parse_body`/`sanitize` for the `capture_fb_*`
wrappers).

## What gets captured vs what gets minted live

| Platform | Transport | Persisted skeleton | Live at replay |
|---|---|---|---|
| Facebook | `POST /api/graphql` form body (`fb_api_req_friendly_name`, `doc_id`, `variables=` JSON) | `{params, variables}` in `agent_reach/channels/_fb_<name>_vars.json` | `fb_dtsg`, `lsd`, `jazoest`, `__dyn`, `__csr`, `__hs*`, `__spin_*` via `window.require("getAsyncParams")("POST")` (an older design replayed a captured `params` template; it broke on every deploy — see `_facebook_share.py`) |
| X | `GET|POST /i/api/graphql/<queryId>/<Operation>` JSON body or query params + auth headers | `{platform, operation, method, query_id, path_template, headers_template, body_template}` in `agent_reach/channels/_x_<op>_vars.json` | `authorization` Bearer (`__wr('973526').Rb()` — public constant), `x-csrf-token` (live `ct0` cookie), `x-client-transaction-id` (`__wr('265390').kc('x.com', path, method)`), cookies via `credentials: include` |

For X, `headers_template` keeps the static `x-twitter-*` constants verbatim
and stores **placeholders** for `authorization`, `x-csrf-token`,
`x-client-transaction-id`, `x-guest-token`; `cookie`/`set-cookie` are
dropped and any unknown `*token*`/`*auth*`/`*secret*` header is
`<redacted>`. `body_template` always holds `{variables, features, queryId}`
— for GET ops (HomeTimeline, UserTweets…) replay puts them back in the
query string.

## Capturing a new mutation

### Facebook (headed opencli profile)

```bash
uv run python scripts/capture_mutation.py --platform fb \
    --profile <opencli-profile-with-facebook-login> \
    --name UFILikeMutation --seconds 90
```

1. The script opens facebook.com in the profile's **headed** browser, arms
   an XHR/fetch interceptor, then polls for POST bodies to `/api/graphql`.
2. Do the action once by hand (click Like, post a comment, share…). Every
   captured `fb_api_req_friendly_name` is printed as it arrives.
3. Matching bodies are parsed; `variables` is **sanitized** and written to
   `agent_reach/channels/_fb_<name>_vars.json` (override with `--out`).

### X (dedicated browser, CDP 127.0.0.1:9222)

```bash
uv run python scripts/capture_mutation.py --platform x \
    --name CreateTweet --seconds 60
# or only list ops while you browse:
uv run python scripts/capture_mutation.py --platform x --list-only
```

1. The script finds (or opens) an x.com tab in the dedicated browser —
   which already holds the logged-in session — and arms
   `window.__cap` via `Runtime.evaluate`. (`Page.addScriptToEvaluateOnNewDocument`
   doesn't run in the main world on CloakBrowser, so arm on the loaded
   page; SPA navigations won't kill it, but a full reload will.)
2. Scroll the timeline / navigate in the SPA / perform the action once.
   Every `/i/api/graphql/<queryId>/<Op>` request is printed as
   `GET:HomeTimeline`, `POST:CreateTweet`, …
3. Matching captures are parsed; headers and body are **sanitized** and
   written to `agent_reach/channels/_x_<op>_vars.json`.

## Sanitize rules (`capture_mutation.py:sanitize`)

Shared, both platforms: `actor_id` / `__user` / `user_id` / `uid` / `av`
→ `"0"`, any UUID → zero-UUID (`idempotence_token` keeps its `_FEED`
suffix, `*session_id` keys → zero-UUID), epoch seconds/ms (12–13 digits
starting with `1`) → `0`, `attribution_id_v2` → fixed template. X callers
add platform keys — `rest_id`, `tweet_id`, `in_reply_to_tweet_id`,
`quoted_tweet_id`, `conversation_id`, `community_id`, `list_id`,
`media_id`, `focal_tweet_id` → `"0"` and credential keys (`auth_token`,
`ct0`, `bearer`, `api_key`, `guest_token`, …) → `<redacted>` — passed as an
extra set so FB output stays byte-identical.

Everything else — including all `__relay_internal__pv__*` flags (FB) and
`features` flags (X) — is kept verbatim; replay needs the full set or the
platform returns `missing_required_variable_value` (FB error 1675012) or
generic variable errors (X).

## Re-capturing when things rotate

- `doc_id` / `queryId` rotates on deploys → re-capture (story 12.2 owns the
  externalized id config).
- **Never reuse another account's skeleton for headers/tokens** — the
  skeleton is safe to share precisely because secrets were scrubbed.
- Never hand-edit a skeleton without re-verifying — run the matching
  `tests/test_*_capture.py` plus the write action's live check.

## Lessons that cost a capture session

- Watch the network tab first and pick the right `fb_api_req_friendly_name`
  or `<Op>` name — FB fires lookalikes (`useCometUFI*` vs
  `CometUFIFeedback*`); X op names map 1:1 to the URL path segment.
- FB capture needs a headed profile (a human clicks); X capture runs on the
  dedicated headless browser — drive it by scrolling/navigating via
  `Runtime.evaluate`, not by hand.
- The skeleton is account-agnostic after sanitization; tokens/headers are
  re-minted live per account.
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

- The dedicated browser must hold a **live session** — FB: check
  `agent-reach facebook profile <someone>`; X: tab on x.com should render
  the logged-in timeline (no login wall). If actions fail with NEED_LOGIN /
  empty results, re-login (`agent-reach login facebook|twitter`) or inject
  fresh cookies via CDP `Network.setCookie`.
- Prefer `only_me` audience for test posts — and clean up test posts from
  the timeline afterwards (they accumulate).
- After an `UPSTREAM_BROKEN`, opencli may pause the channel for ~2 minutes
  (circuit breaker). Wait and retry — don't re-fire mutations blindly,
  some are idempotent but posts/comments are not (duplicates).
- Verify on the page itself, not just the action's exit code: permalink
  loads, reaction count flips, comment appears/disappears.
