---
epic: epic-facebook-write-actions
date: 2026-11-21
verdict: accepted
criteria: declared
headless: false
---

# Retrospective — initiative `facebook-write` (2 epics gộp)

## Epic summary

Retro gộp theo yêu cầu user, neo vào folder `epic-facebook-write-actions` vì đây là epic chính; sibling `epic-messenger-write` được cover cùng document.

### Epic 1 — `epic-facebook-write-actions` (covers R1-R4, R6, R7)

| Ref | Ticket | Status | Plan | baseline | Range |
|---|---|---|---|---|---|
| 1.1 | Mutation capture harness | built | story-mutation-capture-harness-plan.md | (không có — built trước bmad-build-auto, code trong eb95d91) | không đo được |
| 1.2 | facebook react / unreact | built | story-facebook-react-unreact-plan.md | eb95d91 | eb95d91..2e926b1 |
| 1.3 | facebook comment create + delete | built | story-facebook-comment-create-delete-plan.md | 2e926b1 | 2e926b1..d972494 |
| 1.4 | facebook post edit + delete | built | story-facebook-post-edit-delete-plan.md | d972494 | d972494..caf2d46 |
| 1.5 | facebook share | built | story-facebook-share-plan.md | d972494 (worktree `story-1.5-share`, merge caf2d46) | d972494..caf2d46 (qua merge) |
| 1.6 | Write-surface docs update | built | story-write-surface-docs-update-plan.md | caf2d46 | caf2d46..74e76c7 |
| 1.7 | Refactor sweep | built | story-refactor-sweep-plan.md | 74e76c7 | 74e76c7..9c8ed38 |

### Epic 2 — `epic-messenger-write` (covers R5, R6, R7)

| Ref | Ticket | Status | Plan | baseline | Range |
|---|---|---|---|---|---|
| 2.1 | Messenger send transport (spike) | built | spike-messenger-send-transport-plan.md | d972494 | docs trong d972494..caf2d46 |
| 2.2 | facebook message send | **dropped** (spike verdict: non-replayable — E2EE Armadillo qua DGW websocket LSPlatform, ciphertext cần MAW WASM device keys) | story-facebook-message-send-plan.md (stub) | — | không có diff |
| 2.3 | Messenger docs / descope note | built | story-messenger-docs-hoac-descope-note-plan.md | (oneshot, không ghi) | docs/fb-messenger.md trong d972494..caf2d46 |

Tất cả tickets finished (built/dropped) — `pending_tickets` rỗng; không cần retro-over-unfinished. Tickets ở trạng thái `built` (review), chưa `done` — 9 cái, list trong bảng trên.

### Evidence inventory

- **Có**: epic files (đã đọc full, Requirements + Done when declared), plan files x8 (frontmatter, intent contract, Review Triage Log, Auto Run Result), `tickets.py status/find`, git_evidence 5 ranges, session logs = conversation hiện tại (nguồn chính của process lessons, được ghi trong các compress blocks), live e2e artifacts (permalinks, reaction counts, comment ids, post edit/delete verify trong session).
- **Thiếu**: baseline_revision cho 1.1, 2.2, 2.3 (1.1 built trước bmad-build-auto — không có diff range riêng, code nằm trong eb95d91); story files chỉ có cho 1.1 và 1.3 (pull tạo), các story khác build thẳng từ plan; không có retrospective trước đó (epic đầu tiên retro).
- **Ghi chú**: story 1.5 base d972494 (worktree branch khi develop chưa commit 1.4) → share module không chứa `_sent` sentinel/hardening của 1.4 lúc build, merge caf2d46 hợp vào sau; farm-initiative commits (docs/spikes) đan xen trong ranges — lọc ra khỏi scope retro này.

## Findings

### Spec-to-implementation reconciliation

**Epic 1** (requirements đọc từ epic file):

| Req | Spec | As-built | Verdict |
|---|---|---|---|
| R1 | `react --type` + `unreact`, 6 loại | `CometUFIFeedbackReactMutation` doc_id 27646120298312844; 6 reaction ids live-verified; unreact = `feedback_reaction_id:"0"` | ✅ match |
| R2 | comment create + delete own | `useCometUFIAddCommentMutation` + `useCometUFIDeleteCommentMutation`; delete nhận permalink `?comment_id=` hoặc raw base64/base64url Relay id | ✅ match |
| R3 | post edit/delete own, delete guard | `ComposerStoryEditMutation` + `useCometFeedStoryDeleteMutation`; delete yêu cầu flag `confirm` (bool Param — plumbing mới trong cli.py+dispatch.py) → BAD_INPUT trước browser | ✅ match |
| R4 | share, audience default only_me | reuse `ComposerStoryCreateMutation` với `composer_type:"share"` + `attachments[0].link.share_scrape_data` | ✅ match |
| R5 | replay + live tokens + typed errors + fire-and-poll | pipeline chung qua `send_mutation`/`classify_result`; 1 divergence: share lấy envelope live qua `require("getAsyncParams")`, 4 module kia replay params template | ✅ match + 1 deviation |
| R6 | docs không còn "read-only" | 5 file docs sửa ở 1.6; wording mới "Read-first / only Facebook write" | ✅ match |

**Epic 2**:

| Req | Spec | As-built | Verdict |
|---|---|---|---|
| R1 | `facebook message` nếu replayable | Spike chứng minh **không** replayable → 2.2 dropped đúng constraint "if not → descope" | ✅ accepted deviation (spec cho phép) |
| R2 | same write standards | n/a — không build verb | ✅ n/a |
| R3 | docs khớp outcome | `docs/fb-messenger.md` + plan files ghi verdict + evidence | ✅ match |

**Done when** (epic 1): live verbs verify ✅ (react/comment/post-edit/post-delete/share đều chạy live trong session); capture harness ~1 op ✅; unit tests 793 xanh ✅; docs ✅; delivery merge+push `main` @ 9c8ed38 ✅.

**Done when** (epic 2): verdict viết ✅; descope docs ✅; delivery merged ✅.

### Aggregate views

2 subagents opus — aggregate-views analyst (current state, 6 views, evidence-only) + cross-boundary diff lens (`/tmp/retro_epic_diff.txt`, ~5936 dòng, seams between stories).

**Aggregate views (evidence, file:line):**

1. **Import DAG sạch, không cycle**: `_facebook_write.py` = shared-infra root (12 helpers) *và* verb `post` — dual role (361 LOC). `_facebook_react.py` export URL/nav helpers (`_check_url`, `_ensure_post_loaded`, `_post_id_of`, `_same_post_path`, `_resolve_feedback_id`) mà 3 sibling import. `facebook.py` lazy-import 8 entry points trong method. Capture scripts import private helpers từ production + `sys.path.insert` chéo script-với-script → fragile khi move file. *(accept)*
2. **Residual duplication sau 1.7** (vẫn còn đáng kể): JS send-template shape lặp ~6× (~53-66 dòng JS mỗi: `window.require` → tokens → `URLSearchParams` → `p.set` transport → `_sent` sentinel → fetch → parse → `window[KEY]`). Sentinel `.replace` chains 5-7 lệnh/module, order load-bearing (`__FB_TEXT` luôn LAST). `_check_graphql` re-implement 2× (comment vs post_edit); `post_edit._post_id_from_url` copy verbatim `react._post_id_of`; orchestration scaffold (`prepare_facebook`+deadline+`_fb_target`+`_wait_ready`) lặp 5× — chỉ post_edit factor ra `_load_target`. *(defer — A6)*
3. **Parameter-source divergence — confirmed**: share dùng `require("getAsyncParams")("POST")` live envelope (inline comment `_facebook_share.py:78-81` ghi lý do: template stale → `field_exception`); 4 module kia replay `_PARAMS_TEMPLATE` tĩnh. Hệ quả: `_PARAMS_TEMPLATE` ở share là **dead binding** + `params` block trong `_fb_share_vars.json` dead luôn. Divergence chỉ document ở inline comment, chưa có trong `docs/fb-mutation-capture.md`. *(→ A1, nâng mức ưu tiên)*
4. **Skeleton naming/layout lệch**: `_facebook_vars.json` phá convention `_fb_*_vars.json`; `_fb_post_vars.json` layout `{edit,delete}` không có top-level `params`; `_fb_comment_vars.json` `{params,create,delete}`; `_fb_share_vars.json` chứa dead `params` block. doc_id+friendly nằm trong JSON (post_edit) nhưng code đọc `_DOC` hardcode → skeleton chứa data không dùng. *(→ A2, A6)*
5. **God-class `_facebook_post_edit.py` 628 LOC** (18 defs + 5 JS templates ~215 dòng): size đến từ 2 mutations + resolvers + ownership + privacy-preserve — cohesive nhưng ôm nhiều vai trò nhất. `_facebook_comment.py` 480 LOC tương tự. *(accept — chưa cần tách; revisit ở A6)*
6. **Test-harness copy 5×**: cùng `harness` fixture shape ~50-100 dòng/file, không shared `conftest.py`; policy diverge (unknown-expr assert-fail ở write/react/share vs benign `return True` ở comment/post_edit); `__fbPostResult_*` prefix share giữa write + post_edit; `test_facebook_post_edit.py` (778) > module (628). *(defer — A7)*

**Cross-boundary diff lens — 5 findings:**

| # | Finding | Severity | Disposition |
|---|---|---|---|
| F1 | `confirmed` chỉ wire ở post-edit — FB gắn non-fatal `errors[]` vào payload thành công → 4 verb kia báo UPSTREAM_BROKEN → retry → duplicate comment/share/post | medium | **fix-now ✅** (`9a4542d` — wire `confirmed=` cho post/share/comment/react) |
| F2 | `_LOCATE_JS` comment-binding là **dead code**: `btoa("comment:"+cid)` không bao giờ match `btoa("comment:<post>_<cid>")` → react/comment trên comment permalink **silently ghi vào post cha** | medium-high | **fix-now ✅** — base64-node scan (pattern `_COMMENT_LOCATE_JS`) + `cidBound=false` → fail-closed thay vì fallback post fid |
| F3 | `share_params` emit string `["<id>"]` thay vì number `[<id>]` như capture (FB tolerant — e2e pass nhưng là divergence) | low | **fix-now ✅** — splice raw (post_id guaranteed `[0-9]+`) |
| F4 | `_fb_react_vars.json` còn real `downstream_share_session_origin_uri` (uid+post id thật) — identity leak dù runtime overwrite | low | **fix-now ✅** — sanitize `""` |
| F5 | `_fb_target` match host substring `"facebook.com" in url` (match cả `facebook.com.evil.tld`) | low | defer — A8 parse hostname bằng `_FB_HOST_RE` |

**Checked and clean** (diff-lens): `send_mutation` contract nhất quán; `_sent` sentinel trước `fetch(` ở mọi template; placeholder ordering an toàn (`__FB_TEXT` last); skeleton params không chứa secret (fb_dtsg/lsd/jazoest/av/__user live-inject); bool-Param seam strict (`"false"`/int reject); `query_slot` `fcntl.flock` serialize cross-process; `cloak.py __main__` contract đúng.

**Settled**: deferred finding từ story 1.2 ("`_LOCATE_JS` có thể miss escaped feedback_id") — đã settle: dual-pattern regex hoạt động đúng, nhưng review retro phát hiện binding logic hoàn toàn dead code (F2) — bug thật nặng hơn dự kiến, fix-now.

## Behavior verification

Live e2e trên tài khoản thật (uid 61554949615037, dedicated browser, session restore qua cookie injection):

- **post** ✅ — nhiều lần, mới nhất `122311742768164987` ("story 1.7 refactor smoke 🧪", only_me).
- **react** ✅ — love + wow, `reactionId` echo đúng, count 1. **unreact** ✅ — count 0.
- **comment** ✅ — create + delete permalink + delete raw-id, `removed:true`, `dataKeys:["comment_delete"]`.
- **post-edit** ✅ — ASCII + tiếng Việt + emoji (`textEchoed` unicode fix); **post-delete** ✅ — `deletedStoryId` echo, `--confirm` guard → BAD_INPUT khi thiếu flag.
- **share** ✅ — only_me + friends (subagent live e2e).
- **message** — n/a (descoped, không có verb).
- **Suite** ✅ — 793 passed (~60s) sau merge 1.5 + refactor 1.7.

Sự cố session trong quá trình chạy: FB hết hạn 2 lần (checkpoint cookie → invalidate `c_user`/`xs`) — phải re-login Comet rồi inject lại; circuit breaker opencli pause ~120s sau UPSTREAM_BROKEN — cần backoff khi retry e2e.

## Previous-retro follow-through

Không có — đây là retrospective đầu tiên.

## Action items

| # | Item | Status | Owner |
|---|---|---|---|
| A1 | Thống nhất nguồn transport params — adopt `require("getAsyncParams")` (cách share dùng, khỏi hẳn params template) cho 4 module còn lại, hoặc document rõ vì sao giữ template | defer → next initiative | — |
| A2 | Đổi tên skeleton cho nhất quán (`_facebook_vars.json` → `_fb_post_create_vars.json`, `_fb_post_vars.json` → `_fb_post_edit_vars.json`) + update loaders | defer | — |
| A3 | Bổ sung docs/e2e-playbook: điều kiện live verify (session trong dedicated browser, `agent-reach login facebook` khi hết hạn, circuit-breaker pause ~120s sau UPSTREAM_BROKEN, cần backoff) | defer → docs pass tiếp | — |
| A4 | Đưa 2 lesson vào `docs/fb-mutation-capture.md`: dual-pattern `&quot;` escape cho HTML-embedded JSON; unicode-echo (`\uXXXX` escapes trong response body) khi verify text | defer | — |
| A5 | Messenger DM qua DOM-composer path — giữ descoped, chỉ revisit nếu có MAW-provisioned profile; ghi vào deferred-work | defer (research-grade) | — |
| A6 | Dedupe pass 2: hoist JS send-template + sentinel-replace chain + orchestration scaffold (pattern `_load_target` của post_edit) vào `_facebook_write.py`; gộp `_check_graphql` 2× + `_post_id_of` copies; xử lý luôn dead `params` binding của share + skeleton layout lệch (A2 merge) | defer → next initiative | — |
| A7 | Test harness: extract shared `conftest.py` (fixture `harness`, POLL/SEND/LOCATE routing, unknown-expr policy nhất quán); rename prefix `__fbPostResult` cho post_edit | defer | — |
| A8 | `_fb_target`: parse hostname thay substring match, reuse `_FB_HOST_RE` | defer | — |

## Decision records

| Decision | Lúc ra | Outcome | Lesson |
|---|---|---|---|
| Chia epic messenger riêng khi transport chưa rõ | planning | Đúng — spike chứng minh non-replayable, epic 1 không bị chặn | Split uncertainty thành epic riêng có spike-gate |
| Descope comment edit | planning | Đứng — không ai cần | — |
| Parallel worktree cho 1.5 (share) cùng lúc 1.4 trên develop | execution | Worked nhưng có cost: merge conflict `facebook.py` (resolve giữ cả 2 actions) + base 1.5 cũ hơn 1.4 (share module thiếu `_sent` sentinel/sentinels hardening, phải sync lại `_facebook_react.py` từ main repo) | Worktree song song trên cùng file `facebook.py` + cùng file shared (`_facebook_react.py`) → conflict gần như chắc chắn; giá trị vẫn cao vì module mới độc lập |
| Drop 2.2 sau spike verdict | execution | Đúng theo spec ("if not replayable → descope") | Spike-before-story tránh implement hụt |
| `_sent` sentinel + per-call uuid result key (sau review 1.4) | review | Giải quyết duplicate-fire race trên `cdp.call` retry | Fire-and-poll cần sentinel phía JS, không chỉ phía Python |
| `getAsyncParams` live envelope (share) thay params template | execution (1.5) | Share bỏ hẳn params template — robust hơn khi FB rotate build | Đây có thể là pattern tốt hơn template — cần propagate (A1) |
| e2e unicode echo false-negative (`t.indexOf(JSON.stringify(text))` miss `\uXXXX` escapes) | e2e 1.4 | Fix bằng escape-aware check | Response-body substring verification mong manh — prefer parsed fields |
| Retro diff-lens trên aggregate epic diff | retro | Bắt được F2 (wrong-target write dead-code binding) + F1 (confirmed chưa wire 4/5 module) mà 7 lần per-story review không thấy | Per-story review nhìn từng diff — seam giữa module chỉ lộ khi đọc aggregate; retro review có giá trị thật, không chỉ thủ tục |

## Acceptance verdict

**accepted** — tất cả tickets finished (9 built + 1 dropped đúng spec), requirements đáp ứng hoặc có documented deviation (messenger non-replayable được spec cho phép), 793 tests xanh, 8 verb live-verified, retro fix-now commit `9a4542d` vá 4 seam findings thật (1 medium-high wrong-target).

## Open questions

- Retro fix-now commit `9a4542d` + retro doc này chưa push — push khi user approve (develop + ff main).
- Story 1.5 merge cost xác nhận pattern: parallel worktree trên cùng `facebook.py` + shared module → conflict gần như chắc chắn. Nếu epic tiếp theo vẫn parallel: phân chia file boundary trước hoặc chấp nhận merge cost như phí song song hoá.
- `cidBound` fail-closed: trên comment permalink mà page không render comment node (chưa load phần comment) → UPSTREAM_BROKEN thay vì wrong-target — đúng trade-off, nhưng nếu user thấy fail nhiều có thể cần scroll-to-load trước locate.
