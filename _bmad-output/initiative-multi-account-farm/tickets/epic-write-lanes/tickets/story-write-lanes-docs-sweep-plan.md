---
title: 'Write-lanes docs + sweep'
type: feature
ticket: '8'
created: '2026-11-22'
status: built
baseline_revision: '0691bab'
route: oneshot
route_source: auto
risk: low
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Stories 12.4–12.7 shipped the X write surface (`post`, `reply`, `quote`, `thread` — CreateTweet replay in the dedicated browser, text-only) plus write→ledger recording, but every user-facing doc still claimed "read-only except Facebook" / "不提供发帖动作". Docs must match real capability.

**Approach:** Docs-only sweep. Verify the real surface in code first (twitter.py `actions`, `_twitter_write.py` docstring + media descope, `dispatch.py::_record_farm_write`), then update each stale line in place — no new files, no restructuring.

**Surface documented:** X write verbs `post`, `reply <url>`, `quote <url>`, `thread` (≥2 texts post a reply-chain; 1 URL/id still reads). Text-only — media descoped because X uploads run chunked `upload.twitter.com` INIT/APPEND/FINALIZE, not GraphQL. X posts are public by nature — no audience param exists. FB block unchanged (8 verbs + only_me/--confirm defaults from story 1.6).

## Boundaries & Constraints

**Always:** no line may still claim X is read-only; FB wording keeps the "Read-first" pattern; every new claim must be backed by code.

**Never:** no code behavior changes; no new docs; `docs/farm-account-contract.md` untouched (ledger contract is generic and already accurate); no claim of media support or X audience control.

## Verification

**Commands:**
- Grep sweep: `read-only|Read-only|chỉ đọc|只读|不提供发帖` across README/docs/skill/onboarding → no X read-only claim remains.
- `uv run --extra dev python -m pytest tests -x -q` — baseline 966 (docs-only, no test changes expected).

## Auto Run Result

- **Summary**: replaced every stale "read-only except Facebook / 不提供发帖动作" claim with the real X write surface (post/reply/quote/thread, text-only, public-by-default) and kept FB's 8-verb + safety-defaults wording intact; `docs/multi-account-farm-architecture.md` write-lane section now records the shipped X lane + media descope + ledger hookup.

- **Files + lines changed**:
  - `agent_reach/skill/SKILL.md:9-11` — frontmatter description: "仅 Facebook 支持写操作" → writes on FB (8 verbs, only_me/--confirm) + X (post/reply/quote/thread, 纯文本无媒体, 帖子天然公开).
  - `agent_reach/skill/SKILL.md:20` — intro line: "读为主（Facebook 支持写…）" → "读为主（Facebook、X 支持写：post/reply/quote/thread、react/comment/share 等）".
  - `agent_reach/skill/SKILL.md:107` — red line: "除 Facebook 外全部只读" → writes limited to FB + X/Twitter; X verbs + ≥2-text reply-chain thread + text-only + public-by-nature, no audience option.
  - `agent_reach/skill/SKILL_en.md:10` — frontmatter description: "Only Facebook supports writes" → FB writes + X post/reply/quote/thread, text only, public by nature.
  - `agent_reach/skill/SKILL_en.md:19` — intro line: "Read-first (Facebook supports writes…)" → "writes on Facebook and X".
  - `agent_reach/skill/SKILL_en.md:104` — red line: "Read-only except Facebook writes" → "Writes are limited to Facebook and X/Twitter; every other platform is read-only" + X verb list + no-media + no-audience.
  - `agent_reach/skill/references/capabilities.md:20` — X/Twitter row: "search/profile/tweets/thread" → split 读 (search/profile/tweets/thread) vs 写 (post/reply/quote/thread, ≥2-text chain, text-only, public).
  - `agent_reach/skill/references/capabilities.md:23` — Facebook row: added the 8 write verbs + only_me/--confirm defaults (row previously listed only reads).
  - `README.md:110` — twitter row: added "写：发帖、回复、引用、发串（纯文本，帖子公开可见）".
  - `docs/README_en.md:110` — twitter row: added "writes: post/reply/quote/thread (text only, posts are public)".
  - `agent_reach/runtime/onboarding.py:36` — `_CAPABILITY_LIMITS['twitter']`: removed "不提供发帖动作"; now states write verbs, text-only, public, login required.
  - `agent_reach/runtime/onboarding.py:55` — `_QUICK_ACCESS['twitter']` login column: added 发帖回帖.
  - `docs/multi-account-farm-architecture.md:46-51` — "Twitter channel read-only" → shipped X write lane (`_twitter_write.py`, CreateTweet in-page fetch, media descope reason) + `REACH_ACCOUNT` → `farm.record_action` ledger note.
  - `docs/multi-account-farm-architecture.md:84` — roadmap row 1: marked post/reply/quote/thread ✅ (text-only), media remains (descoped — chunked upload, not GraphQL).

- **Grep sweep**: `read.?only|chỉ đọc|只读|不提供发帖` over README/docs/skill/onboarding → only hits are (a) `farm-account-contract.md:154` ("`age` is read-only" — a field, not X), (b) the new SKILL red lines which explicitly exclude X/Twitter from read-only. No X read-only claim remains.

- **Verification**: `uv run --extra dev python -m pytest tests -x -q` → **966 pass** (baseline unchanged; docs-only).

- **Deviations**: also updated `agent_reach/skill/references/capabilities.md` and `docs/multi-account-farm-architecture.md` (not in the ticket's file list) — both contained lines describing X capability that would otherwise still under-describe or mis-state the write surface. `docs/farm-account-contract.md` intentionally untouched: its ledger/write wording is generic and already accurate.

- **Residual risks**: none beyond drift — the next write verb added must be reflected in the same 6 files.
</intent-contract>
