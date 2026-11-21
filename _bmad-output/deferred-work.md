# Deferred work

Items parked from retrospectives — each links to where the rationale lives.

## Messenger DM send via DOM composer — descoped (A5)

- **Source**: initiative `facebook-write` retro (2026-11-21), story 2.2 dropped.
- **Verdict**: keep descoped — not shippable.
- **Why**: DM send has no replayable HTTP/GraphQL mutation. Sends go over DGW
  websocket `wss://gateway.facebook.com/ws/lightspeed` as binary LSPlatform
  task envelopes (stateful `epoch_id`/`task_id` ordering); all personal
  threads are E2EE Armadillo/MAW — ciphertext produced in a WASM worker with
  per-device keys the page never exposes. Driving the composer via DOM/CDP
  `Input.*` was judged research-grade: violates the "no DOM automation"
  boundary, self-DM is broken (profile not MAW-provisioned), fragile to A/B UI.
- **Revisit condition**: a MAW-provisioned browser profile exists AND a real
  product need appears; then re-run a transport spike first (FB may ship a
  GraphQL send mutation later).
- **Details**: `docs/fb-messenger.md`, spike evidence in
  `_bmad-output/initiative-facebook-write/tickets/epic-messenger-write/tickets/spike-messenger-send-transport-plan.md`.

## Open questions carried from `facebook-write` retro

- **File-boundary rules for parallel worktrees**: story 1.5 (share, worktree)
  collided in `agent_reach/channels/facebook.py` and was built on a base
  missing story-1.4 hardening. If parallel stories are used again, either
  partition touch files or rebase the worktree before merge.
- **`cidBound` scroll-to-load**: `_LOCATE_JS` comment binding now fails
  closed when the comment node isn't in the DOM. If `?comment_id=` permalinks
  often lack the rendered node (lazy load), add a scroll-to-comment step
  before locate rather than relying on the fail-closed branch.
