# Facebook Messenger — descope note

**Status (2026-11-21): `facebook message` is NOT implemented and NOT replayable.**

## Why (spike 2.1 verdict)

There is no fetchable HTTP endpoint for sending a DM. CDP Network capture on a
logged-in `facebook.com`/`messenger.com` session shows:

- Every `POST /api/graphql` is a **read** query (`MAWSecureThreadQPContainerQuery`,
  `EBMessageMetadataQueryQuery`, `MAWVerifyThreadCutover_*`, ...). No `messaging/send`
  or lightspeed HTTP endpoint exists.
- Sends go over the **DGW websocket** `wss://gateway.facebook.com/ws/lightspeed`
  (app_id `2220391788200892`) as binary-framed **LSPlatform task envelopes** —
  stateful stream keyed by `epoch_id`/`task_id` order, not a replayable request.
- All personal threads are **E2EE (Armadillo/MAW)**: ciphertext is produced by the
  `MAWMainV4WebWorkerBundle` WASM worker with device keys the page never holds.
  Replaying a captured envelope produces unreadable ciphertext with stale device
  state.
- Self-DM test path is also unusable: legacy `/messages/t/<self>` is composer-locked
  (pre-E2EE); self e2ee threads fail "cannot view content" — the profile is not a
  provisioned MAW device for self-chat.

Conclusion: DM send cannot be done with the fire-and-poll GraphQL replay pattern
used by `post`/`react`/`comment`/`post-edit`/`post-delete`/`share`.

## Fallback if DM send is ever needed

Drive the composer through real browser input — `opencli browser <session>`
`open`/`click`/`type`/`keys` (or CDP `Input.*`) on a logged-in tab. This violates
the "no DOM automation as shipped path" boundary, is fragile against A/B UI
rollouts, and needs a MAW-provisioned profile. Documented for completeness;
deliberately **not** shipped as an `agent-reach` action.

## Evidence

Full CDP capture + analysis:
`_bmad-output/initiative-facebook-write/tickets/epic-messenger-write/tickets/spike-messenger-send-transport-plan.md`
