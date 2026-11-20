# Cascading Failure Simulation — Multi-account X farm

## Failure được chọn: X rotate persisted doc-id của mutation post (`CreateTweet`/`CreateNoteTweet`)

**Tại sao là failure xác thực nhất:** đây là sự kiện platform-side *chắc chắn* xảy ra (XActions đã phải patch `endpoint.url` mỗi lần X đổi query ID — chính doc §4a#3 ghi nhận), và nó là failure global-perfectly-correlated: 1 thay đổi ở X đánh vào cả 30–80 account cùng lúc, nên toàn bộ giá trị của kiến trúc per-account (cô lập container/proxy/session) **không cứu được gì** — chỉ chuỗi epic 2→3→4→6 mới gánh. Nó cũng là failure mà toàn bộ "resilience" story của initiative được thiết kế để hấp thụ, nên trace nó sẽ lộ ra chính xác chỗ nào trong ticket tree còn hổng.

## Trigger

X ship web client mới → doc-id cũ của post mutation chết (GraphQL trả lỗi persisted-query-not-found). Mọi `agent-reach x post|reply|quote|thread` trên toàn farm bắt đầu fail cùng lúc.

## Đường lan qua dependency đã khai

1. **epic-account-substrate (R15)** — không liên quan trực tiếp, nhưng registry ledger rolling-24h bắt đầu ghi các write *thất bại* (xem amplifier #4).
2. **epic-write-lanes (R1/R3)** — replay gửi persisted query với doc-id cũ → GraphQL error. Epic này đã externalize doc-id ra config hot-reloadable nên *code* không cần sửa — nhưng **không ai tự động cập nhật giá trị mới**; và quan trọng hơn, **error class của failure này chưa được định nghĩa**: nó có thể bị phân loại là upstream error generic (→ cooldown/backoff sai), hoặc tệ hơn là `PLATFORM_BLOCKED`/`RATE_LIMITED` — mà semantics hiện có của các hint này là "không đổi backend, không retry" (`dispatch.py`), tức **fallback sẽ không bao giờ kích hoạt**.
3. **epic-account-guardrails (R5/R7)** — nếu action bị refuse/fail, ledger vẫn ghi nhận (spec không nói ledger đếm attempt hay success). Caps bị "đốt" bởi một outage không phải lỗi của account → sau khi fix doc-id, account warm-up bị cap ảo trong 24h.
4. **epic-fleet-orchestration (R9)** — fleet manager poll `agent-reach check`: check verify browser/proxy/session/cooldown, **không probe lane viability**. Mọi container vẫn xanh trong khi write chết 100% → tín hiệu duy nhất là drift monitor (epic 6) — single detector cho failure farm-wide.
5. **epic-farm-resilience (R12/R14)** — hai lớp phòng thủ, cả hai có lỗ hổng:
   - **R14 drift monitor** (host-level cron): chỉ probe "các persisted query đang dùng" — tức config của Agent-Reach. Nó *phát hiện* được, nhưng cadence/alert sink chưa spec → chuỗi recovery = alert → human sửa `_x_vars.json` → hot reload. MTTR = độ trễ của con người, trong khi 80 account đang fail.
   - **R12 XActions fallback**: doc §4a#3 tự thừa nhận "XActions phải patch `endpoint.url` khi X đổi query ID". Tức **fallback thừa hưởng đúng failure vừa xảy ra** — endpoint.url của XActions cũng stale cùng thế hệ doc-id. Monitor không probe store của XActions → kể cả sau khi sửa config Agent-Reach, fallback vẫn route vào endpoint chết. Fallback chỉ che được failure của *cơ chế replay* (CDP/page state/token), **không che được failure của *dữ liệu persisted-query*** — mà đây lại là failure được dự đoán rõ nhất trong risk table.

## Amplifiers

- **Global correlation**: doc-id là per-platform, không per-account → isolation per-account vô nghĩa trước failure này; mọi giảm thiểu phải nằm ở lớp config/fallback/monitor dùng chung.
- **Dual config store không sync**: Agent-Reach vars file và XActions `endpoint.url` là 2 bản copy của cùng một dữ liệu platform → drift độc lập, monitor chỉ canh 1 bản.
- **Sequencing**: epic 6 `after`=[2,3,4] nên fallback + monitor + playbook land *sau cùng* — đúng khoảng thời gian farm đang scale qua 20→80 account (gate scale ≥4 container nằm ở epic 4, không chờ epic 6). Playbook "walk-through trước mốc 20 account" không có gate cơ học nào chặn việc scale vượt mốc đó.
- **Ambiguous scope externalize**: R3 nói "persisted doc-ids/queryIds" nhưng epic 2 Done-when không ghi rõ có bao gồm *read* queryIds (search/profile/tweets/thread) không. Nếu reads vẫn hardcode → cùng một rotation giết luôn read surface toàn farm, và monitor không probe thứ không nằm trong config.

## Single points of failure lộ ra

1. **Human doc-id updater** — alert → sửa tay 2 config stores. SPOF về con người cho MTTR.
2. **XActions lane + config của nó** — fallback SPOF, và là SPOF *chia sẻ failure mode* với lane chính.
3. **Drift monitor duy nhất** — host-level 1 cron; nếu nó chết/bug, không ai biết (fleet check không cover).
4. **Registry file** — ledger + identity + caps trong 1 store; doc-id outage kéo dài sẽ ghi rác vào ledger (xem trên).

## Hardening từng chỗ

- Định nghĩa error class riêng (`PERSISTED_QUERY_DEAD`/`UPSTREAM_SCHEMA`) trong contract, route deterministic sang fallback — không để rơi vào hint no-retry.
- Một nguồn doc-id duy nhất sync sang cả Agent-Reach vars lẫn XActions endpoint (hoặc monitor probe cả 2 store).
- Nới scope externalize + monitor sang toàn bộ X queryIds đang dùng (kể cả reads), hoặc descope reads có lý do ghi rõ.
- Spec cho monitor: interval, alert sink, và đưa persisted-query liveness vào `agent-reach check` để health per-container phản ánh lane viability.
- Ledger chỉ đếm action thành công (hoặc đếm attempt riêng) — ghi vào ledger format contract của epic 1.
- Kéo story R14 (drift monitor — chỉ cần config của epic 2) lên sớm; thêm gate "farm ≤N account tới khi fallback+monitor live".

## Findings

1. **`tickets/epic-farm-resilience/epic-farm-resilience.md` (Done-when b1, Boundaries Out + Must-not-change)** — tham chiếu epic number cũ sót lại sau replan 4→6: "enforcement caps/kill-switch của **epic 2**" (thực tế epic-account-guardrails = id 3), "container template **epic 3**" (thực tế epic-fleet-orchestration = id 4), lặp 3–4 chỗ. PO refine sẽ đọc sai dependency. → Đổi "epic 2"→epic 3, "epic 3"→epic 4 trong file này (đã verify các epic file khác dùng đúng số mới).
2. **`tickets/epic-farm-resilience/epic-farm-resilience.md` Done-when bullet 1** — điều kiện trigger fallback không định nghĩa error class nào; doc-id chết có thể bị nuốt bởi semantics "RATE_LIMITED/PLATFORM_BLOCKED → không đổi backend, không retry" của dispatch. → Thêm failure class riêng (vd `PERSISTED_QUERY_DEAD`) + rule route vào fallback trong contract/Done-when.
3. **`tickets/epic-farm-resilience/epic-farm-resilience.md` + `docs/multi-account-farm-architecture.md` §4a#3** — fallback thừa hưởng cùng failure: XActions `endpoint.url` cũng stale khi X rotate doc-id; drift monitor chỉ probe config Agent-Reach. → Yêu cầu single source/sync config cho cả 2 lane, hoặc monitor probe cả XActions store; fallback story phải bao gồm bước cập nhật XActions config.
4. **`tickets/epic-write-lanes/epic-write-lanes.md` Done-when bullet 2** — scope "doc-ids/queryIds externalized" mơ hồ: chỉ write mutations hay cả read queries (search/profile/tweets/thread)? Nếu reads hardcode, X rotation giết read surface toàn farm ngoài tầm monitor. → Ghi rõ scope = toàn bộ persisted queries X đang dùng, hoặc descope reads kèm lý do.
5. **`tickets/epic-farm-resilience/epic-farm-resilience.md` R14/Done-when bullet 3** — drift monitor thiếu cadence, alert sink, và định nghĩa probe target; recovery phụ thuộc human sửa tay → SPOF chuỗi giờ-ngày; đồng thời `agent-reach check` (epic 4) không probe lane viability nên fleet xanh giả. → Spec interval + kênh alert; cân nhắc đưa persisted-query liveness vào `agent-reach check`.
6. **`tickets/epic-account-guardrails/epic-account-guardrails.md` R5 + epic 1 ledger format** — không spec ledger đếm attempt hay success: outage doc-id sẽ đốt write caps rolling-24h của account vô tội → cap ảo + warm-up lệch sau recovery. → Ledger chỉ đếm action thành công (hoặc tách attempt counter), ghi rõ trong ledger contract của epic-account-substrate.
7. **`tickets/tickets.toml` + initiative Done-when** — epic 6 `after`=[2,3,4] nhưng R14 chỉ cần config của epic 2; playbook phải walk-through "trước mốc 20 account" nhưng không có gate cơ học chặn scale — farm có thể chạy 30–80 account chỉ với replay lane, zero fallback, zero monitor. → Tách/kéo story R14 lên ngay sau epic 2; thêm program gate "farm size ≤N tới khi fallback + drift monitor live".