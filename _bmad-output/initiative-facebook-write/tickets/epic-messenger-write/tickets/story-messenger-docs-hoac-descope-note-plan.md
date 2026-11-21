---
title: 'Messenger docs hoặc descope note'
type: feature
ticket: '3'
created: '2026-11-21'
status: built
route: oneshot
route_source: auto
risk: low
---

## Auto Run Result

- Spike 2.1 verdict: DM send **not replayable** (E2EE Armadillo over DGW websocket LSPlatform; ciphertext needs MAW WASM device keys; no /api/graphql send endpoint; self-DM unusable).
- Outcome = descope branch of ticket: wrote `docs/fb-messenger.md` — verdict, technical reason, fallback (opencli browser DOM automation), pointer to spike evidence.
- Story 2.2 `facebook message send` → `dropped` with blocked_reason referencing spike verdict.
- No code change → no tests. Verify: docs exist and reflect spike result. ✓
