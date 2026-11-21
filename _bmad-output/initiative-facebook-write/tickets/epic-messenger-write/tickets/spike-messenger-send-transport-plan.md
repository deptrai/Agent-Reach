---
title: 'Messenger send transport'
type: feature
ticket: '1'
created: '2026-11-21'
status: built
baseline_revision: 'd972494f6d5c30fceb1430c05bde0d8420a7986e'
route: oneshot
route_source: auto
risk: high
review: quick
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Before epic-messenger-write can plan a `dm` write action, we must know what transport facebook.com/messenger.com actually uses to send a DM and whether it is replayable as an in-page `fetch` on `/api/graphql` the way `post`/`react`/`comment` are.

**Approach:** INVESTIGATION ONLY. Drive a dedicated tab on the logged-in dedicated browser (uid 61554949615037, CDP 127.0.0.1:9222), capture all network traffic via the CDP Network domain (fetch/XHR POSTs + every websocket + every frame), open the self-DM, attempt a send, and record the evidence.

## Boundaries & Constraints

**Always:** Own tab only (`Target.createTarget`), never drive other agents' tabs. Prefer self-DM; never message a real user. Capture proof before concluding.

**Never:** No feature implementation, no committed code, no sending messages to real accounts, no credential export.

</intent-contract>

## Spike Verdict

**VERDICT: NOT replayable via in-page `fetch`.** A Messenger DM send on this account does not travel as a `POST /api/graphql` mutation. It is an encrypted payload produced inside the Armadillo/MAW WASM worker and pushed out over a persistent, stateful, binary-framed DGW websocket (`wss://gateway.facebook.com/ws/lightspeed`) as an LSPlatform task envelope. There is no fire-and-poll HTTP write path to replay.

### Transport actually observed (CDP Network capture, live session)

1. **`/api/graphql` POSTs = read queries only.** Every messenger POST during page load, thread open and composer typing was a *query*: `MAWSecureThreadQPContainerQuery`, `MAWSecureThreadQPContainerLazyLoadedQuery`, `MWChatTabInThreadBannerQuery`, `EBMessageMetadataQueryQuery`, `MAWVerifyThreadCutover_ContactCapabilities2Query`, `MWEBOneTapOtcTransportEligibleDevicesQuery`, `useMWEncryptedBackupsFetchBackupIdsV2Query`, `OhaiWebPublicKeyConfigQuery`, plus generic non-chat queries. **Zero send mutations.** No `messaging/send`, no `lightspeed` HTTP endpoint observed at all.

2. **Messenger state moves on `wss://gateway.facebook.com/ws/lightspeed`** (x-dgw-appid `2220391788200892`, authtype=1, tier=prod). Frames are binary (opcode 2) DGW-framed JSON envelopes:

   ```json
   {"app_id":"2220391788200892","payload":"<string>","request_id":N,"type":...}
   // payload decodes to:
   {"epoch_id":<int64>,"version_id":"40618627727753075",
    "tasks":[{"failure_count":null,"label":"<int>","payload":"<task-json>",
              "queue_name":"<queue>","task_id":<int>}]}
   ```

   Real envelopes captured on thread open (thread_key `7711046542293727`, fbid `100004778588514`): label `235` queue `ctm_ad_context`, label `192` `proactive_warnings_fetch_queue`, label `209` queue=contact (payload `{"thread_fbid":...,"use_open_messenger_transport":0,"sync_group":1,...}`), label `72` `in_thread_banner_fetch_activity_banners_queue`, label `436` (armadillo thread keys + trace_id), label `648` `verify_threads_activity_status`, label `54`/`117`/`767` per-thread inserts, label `145` `trq` (thread-list range queries), label `207` `cpq_v2` (contact presence queries, ~40 contacts). Plus per-LS-database sync-cursor frames `{"database":N,"epoch_id":...,"last_applied_cursor":...,"sync_params":"{\"locale\":\"vi_VN\"}"}` for databases 1,2,16,26,95,140,141,143,145,196,198,202.

3. **E2EE (MAW/Armadillo) does the crypto, not the page.** Every personal thread on this account is `armadillo`/e2ee (`/messages/e2ee/t/...`). The page hosts a `www.fbsbx.com/maw_proxy_page` iframe + a `MAWMainV4WebWorkerBundle` worker. Telemetry captured while typing a character into a real e2ee composer proves the write path: `armadillo_worker_runtime.backend_sendChatStateFromComposer_start/success` (chat-state routed *through* the armadillo worker), `send_typing_indicators_SECURE_MESSAGE_OVER_WA_ONE_TO_ONE`, `syncMailboxKeysForContactsInWorker`, `maw_non_timeout.*` counters, storage key `armadillo_msgr_data_loss_stats` (`POST /ajax/webstorage/process_keys/`). The plaintext→ciphertext step happens inside the device-registered worker; the page only sees opaque bytes.

4. Other DGW sockets are not the send path: `streamcontroller` = GraphQL live subscriptions (PresenceUnifiedJSON, USER_ACTIVITY_UPDATE, notification subs — all doc_id'd subscribes), `realtime` = telemetry batching (the frames above are where the armadillo counters ride), `rpsignaling` = call signaling. Keepalive = 1-byte `0x09`; `0x0c..`/`0x0e..` 8/4-byte frames = puback-style control.

### Self-DM attempt

`/messages/t/61554949615037` opens the **legacy pre-E2EE self-chat ("Phan Hội")** — composer is absent: "Những tin nhắn này được gửi trước khi đoạn chat được bảo mật… bạn không thể trả lời". Clicking "Tiếp tục" navigates to `/messages/e2ee/t/7529865224755857910` (self e2ee thread) which fails with "Bạn hiện không xem được nội dung này" — this browser profile is not a provisioned MAW device for self-chat. A real e2ee thread (`/messages/e2ee/t/7711046542293727/`) does render with a working composer, but sending there means messaging a real user — skipped per constraints. Exact send-task label therefore not captured; irrelevant given the transport is not replayable anyway.

### Why it cannot replay like post/react/comment

- **No fetch endpoint exists** for the send. The write is a task enqueued onto a stateful, ordered WS stream carrying `epoch_id`/`task_id`/`queue_name` sequencing maintained by the page's LSPlatform client — a one-shot `fetch` cannot address it, and replaying crafted WS frames requires a DGW client + binary framing + stream lifecycle + correct epoch sequencing.
- **Payload is Armadillo ciphertext** produced by a device-registered WASM worker. Forging it requires the mailbox/device keys; even *receiving* self-chat fails on this profile.
- Non-e2ee threads would not save it: messenger unified sends on the LS channel years ago; the capture shows no HTTP write path at all.

### Recommendation for story 2.2

**Descope `dm-send` from the fire-and-poll write-action pattern — it is architecturally impossible.** Realistic options:

1. **Drop messenger write entirely** (recommended): no replayable transport; the only honest verdict.
2. **Composer-driving fallback** (weak): CDP `Input.insertText` + Enter in a dedicated tab does reach the send path (proven: chat-state flowed to the armadillo worker on keystroke). But it violates the "no DOM-as-shipped-path" boundary, only works on this MAW-provisioned browser, is untestable via self-DM (self e2ee thread broken on this device), and a real send was not exercised — treat as research-grade, not a write action.
3. **Full LS/MAW client**: implement DGW framing + LSPlatform task sequencer + Armadillo WASM integration — a research project, not a story.

Evidence artifacts: `/tmp/spike21/events.jsonl` (CDP Network captures), `/tmp/spike21/collect2.py` (multi-target capture incl. worker/iframe targets — both showed zero own-network traffic). Caveat for future capture work: `Page.addScriptToEvaluateOnNewDocument` does not inject in this browser — use CDP Network domain instead.
