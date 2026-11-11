---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [limit]
---
Delete a knowledge entry the game plainly contradicts and where the game is the one that is right. The most destructive call you have: a deleted rule is gone for every later run. One contradiction is more often a bug, so report it with `report_test_case` and keep the entry. To correct, use `update_knowledge`. `knowledge_id` must come from a search hit in this run; `thought` is why it is wrong. A run gets {limit} deletions. Before first use, read the knowledge_base skill.
