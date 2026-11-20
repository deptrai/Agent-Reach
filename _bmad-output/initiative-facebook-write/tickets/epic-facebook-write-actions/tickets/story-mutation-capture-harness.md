---
id: 1
type: story
title: "Mutation capture harness"
parent: epic-facebook-write-actions
covers: [R5]
after: []
hitl: true
risk: medium
---

# Mutation capture harness

## Description

Script/helper capture bất kỳ /api/graphql mutation body từ dedicated browser thành skeleton JSON tái dùng: arm interceptor qua CDP eval, người dùng thực hiện action một lần, dump request body có friendly name, sanitize (xoá actor_id/uuid/session fields) và lưu dạng *_vars.json. Kèm doc ngắn về quy trình re-capture khi doc_id rotate.

## Requirements

Verify: Capture một mutation thật (ví dụ like) tạo ra skeleton JSON load được và pass test sanitize.

## References

- parent — _bmad-output/initiative-facebook-write/tickets/epic-facebook-write-actions/epic-facebook-write-actions.md
