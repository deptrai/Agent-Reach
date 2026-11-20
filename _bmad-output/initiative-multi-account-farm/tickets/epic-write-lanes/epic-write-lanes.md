---
id: 1
type: epic
title: "Write lanes replay mutation"
parent: initiative-multi-account-farm
covers: ["R1", "R2", "R3", "R4"]
sources: []
companions:
  - docs/multi-account-farm-architecture.md
after: []
refined: false
risk: high
---

# Write lanes replay mutation

## Description

Đưa write surface của Agent-Reach từ "chỉ Facebook post" lên đa platform, theo replay pattern `_facebook_write.py` đã chứng minh: capture mutation thật → skeleton params → replay trong dedicated browser với live tokens. Nền móng là externalize persisted doc-ids/queryIds ra config hot-reloadable — mọi lane build trên đó, và X đổi doc-id thì sửa file chứ không sửa code.

## Outcome

Người vận hành farm gọi `agent-reach x post|reply|quote|thread` (kèm media) và verify được kết quả trên platform thật; thêm platform mới = chạy capture pipeline + thêm config, không phải reverse-engineer lại.

## Done when

- `agent-reach x post|reply|quote|thread` (+ media nếu pattern cho phép) verify live được trên X.
- Doc-ids/queryIds nằm trong config/data file hot-reloadable; `agent-reach doctor` (hoặc tương đương) cảnh báo được drift.
- Mutation capture pipeline tổng quát hóa từ `docs/fb-mutation-capture.md` dùng được cho ≥2 platform (FB đã có + X).
- Write lanes cho LinkedIn/Reddit/Instagram/TikTok theo thứ tự ưu tiên farm (hoặc ghi descope có lý do — ví dụ TikTok chưa có channel).

## Boundaries

- Out: guardrails (caps/warm-up/kill-switch — epic 2), container/fleet (epic 3), XActions fallback (epic 4), Facebook write surface (initiative-facebook-write).
- Must not change: read actions của `twitter.py` và các channel read hiện có; `_pace_browser`/cooldown semantics trong `dispatch.py`.
- Touch point: `agent_reach/channels/_facebook_write.py` + `_facebook_vars.json` — pattern mẫu; `docs/fb-mutation-capture.md` — capture pipeline mẫu.

## References

- parent — `initiative-multi-account-farm.md`, Requirements R1–R4
- architecture — `docs/multi-account-farm-architecture.md` §4a (work items 1–4), §3 (replay constraint C4)
- code — `agent_reach/channels/_facebook_write.py`, `agent_reach/channels/twitter.py`, `agent_reach/runtime/dispatch.py`
- docs — `docs/fb-mutation-capture.md`
