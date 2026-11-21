---
type: initiative
title: "Multi-account X farm trên Agent-Reach"
sources:
  - docs/multi-account-farm-architecture.md
companions:
  - docs/multi-account-farm-architecture.md
after: []
risk: high
---

# Multi-account X farm trên Agent-Reach

## Description

Agent-Reach hiện chạy single-account trên 1 máy (bridge OpenCLI là singleton `127.0.0.1:19825`, dedicated browser dùng chung). Initiative này biến nó thành farm 30–80+ account X/Twitter: mỗi account sống trong 1 container riêng (HOME riêng, `~/.agent-reach` riêng, residential proxy riêng, bridge riêng trong netns), có write lanes replay mutation thật, guardrails per-account, và orchestration quản lý cả fleet — theo kiến trúc all-Reach đã chốt trong `docs/multi-account-farm-architecture.md`.

## Outcome

Chủ farm vận hành được 30–80 account X song song trên 1 home server với stealth tối đa — fingerprint thật (anti-detect Chromium), IP sạch per-account, hành vi cap theo ngày — và farm không chết hàng loạt khi platform đổi doc-id hay 1 mutation gãy.

## Requirements

- R1 (§4a #1): X/Twitter write lane — `agent-reach x post|reply|quote|thread` + media, replay persisted GraphQL trong dedicated browser, kết quả verify được (id/permalink).
- R2 (§4a #2): Write lanes LinkedIn → Reddit → Instagram theo thứ tự đó, cùng replay pattern; TikTok chỉ sau capture spike chứng minh khả thi (repo chưa có cả read channel).
- R3 (§4a #3): Persisted doc-ids/queryIds externalized ra config/data file hot-reloadable — platform đổi doc-id thì chỉ sửa file, không sửa code.
- R4 (§4a #4): Mutation capture pipeline chuẩn hóa cho mọi platform (record request thật → extract persisted query → sinh `_<platform>_vars.json`).
- R5 (§4b #5): Write caps per-account per-action-class per-day — ledger rolling-24h (model `action-caps` của XActions), env-configurable, enforce trong dispatch trước mọi write.
- R6 (§4b #6): Warm-up profile cho account mới — lịch ramp-up hành vi (ngày đầu read + tương tác ít, post tăng dần).
- R7 (§4b #7): Proxy kill-switch — **zero egress ngoài proxy của account, fail-closed** `PLATFORM_BLOCKED`, không fallback direct, áp cho mọi action (read cũng leak IP); KHÔNG so "exit IP = proxy" vì residential proxy rotate — semantics sửa từ party critique 2026-11-21.
- R8 (§4c #8): Container template (Docker/Podman) per account — HOME riêng, `~/.agent-reach` volume riêng, `REACH_BROWSER_PROXY` riêng, netns riêng, resource cap ~1.5GB RAM.
- R9 (§4c #9): Fleet manager — health per container (browser up? proxy alive? session valid? cooldown state?), poll định kỳ từ output JSON của `agent-reach check`, alert container đỏ.
- R10 (§4c #10): Session refresh/re-login automation — detect `NEED_LOGIN`, mở login window trong đúng container của account đó.
- R11 (§4c #11): Metrics ban-rate — log per-account action/ngày, RATE_LIMITED, PLATFORM_BLOCKED, ban/suspend; dashboard tối giản.
- R12 (§4d #12): XActions HTTP fallback — account tự degrade về `xactions-mcp` HTTP lane khi mutation replay gãy hoặc lane chưa tồn tại.
- R13 (§4d #13): Incident playbook — account bị suspend, proxy pool hỏng, OpenCLI update gãy; phải có trước khi scale >20 account.
- R14 (§4d #14): Doc-id drift monitor — cron probe các persisted query còn sống không, alert trước khi farm lỗi hàng loạt.
- R15: Per-account substrate — registry là source of truth duy nhất (account↔proxy↔container↔session↔tuổi↔ledger rolling-24h↔ban status) + contract danh tính per-run + kill-switch contract một chỗ (thêm từ party critique 2026-11-21 — doc gốc giả định ngầm ở mọi work item nhưng không liệt thành item).

## Constraints

- C1 (§3): Kiến trúc all-Reach single-tier — mỗi account = 1 container riêng (HOME riêng, `~/.agent-reach` riêng, proxy riêng, bridge `127.0.0.1:19825` riêng trong network namespace riêng); không share OS-level bridge.
- C2 (§3, §5): Dedicated browser = anti-detect Chromium fork bắt buộc — fingerprint correlation giữa các container không giải quyết bằng proxy; cấm Chrome/Chromium thường. Vendor: CloakBrowser (upstream-tested nhưng freemium — free key 1 profile, Pro cho scale) hoặc ShardBrowser (MIT, profile API + per-profile proxy native) — chốt bằng spike ở epic-fleet-orchestration.
- C3 (§3): Proxy residential/ISP 1:1 per account với kill-switch fail-closed; cấm datacenter proxy (tín hiệu farm mạnh).
- C4 (§4a, pattern `_facebook_write.py`): Write actions replay mutation thật trong dedicated browser với live tokens; fire-and-poll, cấm blind retry.
- C5 (§3): XActions HTTP lane chỉ là fallback khi replay gãy — Node.js TLS fingerprint ≠ Chrome JA3, không dùng làm lane chính.
- C6 (§3): Budget ~1–1.5GB RAM/account — 64GB đủ ~30–40 account, 256GB dư cho 80+.
- C7 (§5): Pin version OpenCLI/bridge trong `runtime/tools.py`; update test trên 1 canary container trước khi rollout cả farm.

## Done when

- `agent-reach x post|reply|quote|thread` verify live được từ ít nhất 1 account chạy trong container riêng với proxy riêng; X đổi 1 doc-id → chỉ cần sửa config file, không cần sửa code.
- ≥4 container chạy song song trên 1 host chứng minh pattern scale (epic-fleet-orchestration), mỗi cái trong container riêng; fleet manager hiển thị health per container và alert được container đỏ; kèm runbook nhân bản 2→30+.
- Write caps chặn được burst trên 1 account thật (vượt cap → refuse trước khi gọi platform); proxy chết → action fail-closed, không leak IP nhà.
- Drift monitor probe được các persisted query đang dùng và alert khi 1 cái chết; 1 mutation replay gãy → account tự degrade về XActions HTTP lane (vẫn qua guardrails, egress qua proxy của account).
- Incident playbook tồn tại và đã walk-through ít nhất 1 scenario (suspend hoặc proxy pool hỏng) trước khi farm vượt 20 account — epic-farm-resilience land đủ sớm trong program, không đợi cuối hàng.
- Gate xuyên epic: mọi live verify của write lane chỉ trên designated test account không nằm trong farm rotation; account chỉ vào rotation sau khi epic-account-guardrails Done-when.

## Boundaries

- Out: Facebook write surface — thuộc `initiative-facebook-write` đang chạy; tạo/mua/verify account mới; content & persona strategy; ban-appeal automation; Puppeteer lane mới; datacenter proxy.
- Touch point: `agent_reach/channels/_facebook_write.py` + `_facebook_vars.json` — replay pattern mẫu, vars skeleton mẫu; owner: epic-write-lanes.
- Touch point: `agent_reach/runtime/dispatch.py` — hook đọc registry (owner: epic-account-substrate), chỗ gắn write caps + kill-switch soft-check (owner: epic-account-guardrails).
- Touch point: `agent_reach/runtime/onboarding.py`, `bridge.py`, `browser_watcher.py` — login flow, proxy launch kwarg, profile paths; owner: epic-fleet-orchestration.
- Touch point: repo XActions (`../XActions`, branch `main-clean`) — model `action-caps` tham chiếu + HTTP fallback lane; owner: epic-farm-resilience.
- Must not change: read surface hiện có (search/profile/tweets/thread); mô hình auth qua dedicated browser; bridge contract `127.0.0.1:19825`; `_pace_browser`/`RATE_LIMIT_SECONDS` semantics sẵn có (caps per-account bổ sung, không thay thế).
- Tracer path: account substrate (registry + contracts) → externalize doc-ids → X write lane `post` → write caps + kill-switch soft-check → container template + fleet health → platform lanes + resilience.

## References

- architecture — `docs/multi-account-farm-architecture.md` (brief + kiến trúc đã chốt + 14 work item + risk table + thứ tự ưu tiên)
- code — `agent_reach/channels/_facebook_write.py` (replay pattern), `agent_reach/channels/facebook.py` (`login_cookie`)
- code — `agent_reach/runtime/dispatch.py` (`_pace_browser`, `RATE_LIMIT_SECONDS`, `BACKEND_PAUSE_SECONDS`, `query_slot`)
- code — `agent_reach/runtime/bridge.py` (singleton `127.0.0.1:19825`), `browser_watcher.py` (proxy launch, CloakBrowser)
- code — `agent_reach/runtime/session_reuse.py` (`profiles use <id>` = nguồn cookie, không phải môi trường chạy)
- docs — `docs/fb-mutation-capture.md` (capture pipeline mẫu), `docs/cloakbrowser.md`

## Notes

- TikTok: chưa có cả read channel — story đầu của epic-platform-lanes là capture spike; khả thi → build lane, không → descope ghi rõ lý do (chốt 2026-11-21 sau party critique).
- Decision: 2026-11-21 — cắt 4 epic theo 4 nhóm §4 của doc; guardrails tách khỏi write-lanes vì là policy layer cross-platform ở dispatch, không phải lane.
- Decision: 2026-11-21 — không chạy `bmad-architecture` riêng; §3 của doc là architecture spine đã chốt, adopt làm companion cho mọi epic.
- Decision: 2026-11-21 — giữ `initiative-facebook-write` làm active initiative (đang build dở); farm chạy song song, switch khi user quyết định.
- Decision: 2026-11-21 — mua residential proxy pool là hitl entry khi plan epic-fleet-orchestration, không nằm ngoài tree.
- Decision: 2026-11-21 — ~~epic-write-lanes kiêm platform-baseline~~ SUPERSEDED: baseline thật là `epic-account-substrate` mới (registry + contracts) — party critique 5/5 persona hội tụ chỗ này.
- Decision: 2026-11-21 — fallback HTTP egress bắt buộc qua proxy của account (C3, không accept leak IP nhà); lane-side wiring ở epic-farm-resilience, env per-account do epic-fleet-orchestration cung cấp; fallback writes vẫn qua enforcement của epic-account-guardrails.
- Decision: 2026-11-21 — drift monitor chạy host-level 1 instance (doc-id global theo platform); version pin OpenCLI nằm ở container template epic-fleet-orchestration (C7), playbook epic-farm-resilience chỉ cover quy trình canary.
- Decision: 2026-11-21 — REPLAN sau party critique (5/5 persona hội tụ): slice 4→6 epic; thêm `epic-account-substrate` (R15 registry + contracts) làm baseline thật; tách R2 ra `epic-platform-lanes`; thứ tự R2 LinkedIn→Reddit→Instagram, TikTok sau spike; kill-switch contract "zero egress ngoài proxy_i" (dispatch soft + netns hard); live verify gate = designated test account cho tới khi guardrails xong; scale gate ≥4 container + runbook về epic-fleet-orchestration.
- Decision: 2026-11-21 — apply-all round 1 elicitation (`elicitation-farm-slice/`): renumber epic-farm-resilience sang id=6 + dùng slug refs; thêm edge epic3→epic2 và epic4→epic2; verify substrate đổi tuần tự + contract ghi isolation mechanism; contract ledger thêm concurrency model + chỉ đếm success + fallback ghi ledger; externalize gồm cả read queryIds + probe ≥2 account; error class `PERSISTED_QUERY_DEAD` route vào fallback; runbook cap ≤20 tới khi epic 6 live; thêm identity bridge Reach↔XActions; delineate drift doctor-vs-monitor; `age` populate ở epic 1 + ghi engagement warm-up là known gap.
- Decision: 2026-11-21 — C2 nới vendor: CloakBrowser hóa ra freemium (free key 1 profile, Pro cho scale — README CloakHQ) → không phù hợp farm 30–80; mở đường ShardBrowser (`ProxyShard/ShardBrowser`, MIT, profile API + per-profile proxy native). Chốt bằng spike đầu epic-fleet-orchestration: verify OpenCLI extension load + CDP tương thích `dedicated_context` + fingerprint kích hoạt qua đường nào + fingerprint scan.
- Decision: 2026-11-21 — renumber epic ids 1–6 → 11–16 tránh trùng story ref "x.y" với initiative-facebook-write đang build song song (commit c62beef).
