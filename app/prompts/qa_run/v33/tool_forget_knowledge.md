---
version: v33
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [limit]
---
Delete a knowledge entry the game plainly contradicts and where the game is the one that is right. The most destructive call you have: a deleted rule is gone for every later run. One contradiction is more often a bug, so report it with `report_step` and keep the entry. To correct, use `update_knowledge`. `knowledge_id` must come from a search hit in this run; `thought` is why it is wrong. A run gets {limit} deletions. Before first use, read the knowledge_base skill.
