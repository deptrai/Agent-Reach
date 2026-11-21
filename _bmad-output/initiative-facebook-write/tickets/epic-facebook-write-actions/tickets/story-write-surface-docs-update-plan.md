---
title: 'Write-surface docs update'
type: feature
ticket: '6'
created: '2026-11-21'
status: built
baseline_revision: caf2d46
route: oneshot
route_source: auto
risk: low
review: quick
---

## Intent

Docs đang claim "chỉ post" trong khi đã ship 8 write actions. Cập nhật mọi docs
liệt kê write surface thật + safety defaults; gỡ wording "single write exception".

## Write surface thật (8 actions)

`post` · `react` (like|love|haha|wow|sad|angry) · `unreact` · `comment` ·
`uncomment` · `post-edit` · `post-delete` (guard `--confirm`) · `share`

Safety defaults: post/share default `only_me`, audience khác cần user chỉ định;
`post-delete` bắt `--confirm`; tất cả write cần login trong dedicated browser.

## Tasks

1. `agent_reach/skill/SKILL.md` — dòng 9, 19, 104: liệt kê đủ verbs + defaults.
2. `agent_reach/skill/SKILL_en.md` — dòng 10, 19, 104: tương tự (EN).
3. `README.md` — dòng 111 (bảng capability) + 184 (safety bullet).
4. `docs/README_en.md` — dòng 111 + 184.
5. `agent_reach/runtime/onboarding.py` — dòng 39: thêm post-edit/post-delete/share.

## Verification

- Không còn chuỗi "不评论、不点赞" / "never posts, comments, likes" / "single write exception" / "read-only" mâu thuẫn.
- `grep -rn "read-only\|只读" README.md docs/README_en.md agent_reach/skill/` chỉ còn mô tả đúng phạm vi.
