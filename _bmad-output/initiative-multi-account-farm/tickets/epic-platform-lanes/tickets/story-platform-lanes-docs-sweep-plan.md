---
title: 'Platform lanes docs + sweep'
type: feature
ticket: '8'
created: '2026-11-22'
status: built
baseline_revision: 'c2538e4'
route: full
risk: low
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Story 15.4 shipped Reddit `post`/`comment`, but skill red lines, README "read-first" bullets, and the farm architecture roadmap still said writes were Facebook-only (or Facebook+X) and that LinkedIn/Reddit/Instagram/TikTok lanes were one unimplemented pattern.

**Approach:** Docs + skill markdown only. Verified `WRITE_ACTIONS` (`facebook` 8 verbs + `message` listed, `twitter` post/reply/quote/thread, `reddit` post/comment) and `_reddit_write.py` error mapping, then corrected each stale user-facing claim in place.

## Boundaries & Constraints

**Always:** Reddit post+comment is shipped code AND currently CAPTCHA-walled (session-dependent). LinkedIn/Instagram/TikTok stay blocked; 15.2 and 15.6 stay open.

**Never:** no code changes (including `agent_reach/runtime/onboarding.py`); no push.

## Verification

**Commands:**
- Grep sweep of README/docs/skill for the stale "only Facebook" / "every other platform is read-only" / "đang build" lane claims.
- `uv run --extra dev python -m pytest tests -x -q`

## Auto Run Result

- **Summary**: write-capability statements now match the code. Reddit `post`+`comment` is documented as shipped, with the live-verify caveat: legacy `/api/submit` on account `Objective_Bet_368` returns `BAD_CAPTCHA` (r/test, own profile, old.reddit.com); `gql.reddit.com` cross-origin fetch from the page also fails. Error mapping recorded: RATELIMIT→`RATE_LIMITED`, no modhash→`NEED_LOGIN`, validation→`BAD_INPUT`, `BAD_CAPTCHA`→`UPSTREAM_BROKEN`.

- **Still open (operator-gated)**: story 15.2 LinkedIn write (spike 15.1: `NEEDS_OPERATOR_LOGIN` + `linkedin.com` `ERR_CONNECTION_RESET` from this host; no write code) and story 15.6 Instagram write (spike 15.5: CAA account picker, no `sessionid`; `post` descoped, `comment`/`like` viable once logged in). TikTok (15.7) stays login-blocked with video `post` descoped; `X-Bogus` mint via `byted_acrawler.frontierSign` is verified, `like`/`comment` replayable-conditional.

- **Claims corrected**:
  - `agent_reach/skill/SKILL.md` + `SKILL_en.md` — frontmatter, intro, and the red line no longer say writes are Facebook+X only; CAPTCHA caveat added (15.4 had already listed the verbs).
  - `agent_reach/skill/references/capabilities.md` — Reddit row: shipped post/comment, legacy submit CAPTCHA-walled.
  - `README.md` + `docs/README_en.md` — Reddit table row, and the "read-first / 唯一例外是 Facebook" bullets (those also omitted the already-shipped X writes).
  - `docs/multi-account-farm-architecture.md` — comparison-table Write cell, section 2 write-lane paragraph, §4a retitled off "đang build", roadmap row 2 no longer "cùng pattern / unimplemented", new **Platform write lanes** status table.
  - `docs/farm-account-contract.md` — `WRITE_ACTIONS` parenthetical now includes reddit post/comment.

- **Not touched (no stale lane claim, or out of scope)**: `docs/farm-xactions-bridge.md`, `docs/farm-guardrails-playbook.md`, `docs/farm-incident-playbook.md`, `docs/farm-scale-runbook.md`, `docs/farm-drift-monitor.md`.

- **Residual**: `agent_reach/runtime/onboarding.py` still says Reddit "不提供发评论动作" and the quick-access login column does not mention post/comment. Left unchanged because this story is docs + skill markdown only.

- **Deviations**: also fixed the README "only Facebook writes" bullets, which were already wrong about X before this story. No code, no new runtime behavior.
</intent-contract>
