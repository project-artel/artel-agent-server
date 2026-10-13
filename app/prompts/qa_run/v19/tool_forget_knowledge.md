---
version: v19
note: v17 의 FORGET_KNOWLEDGE_DESCRIPTION 을 줄인 것이다. 지우기 전에 update_knowledge 를 먼저 보라는 설명은 knowledge_base skill 로 옮겼다.
placeholders: [limit]
---
Delete a knowledge entry the game plainly contradicts and where the game is the one that is right. The most destructive call you have: a deleted rule is gone for every later run. One contradiction is more often a bug, so report it with `report_step` and keep the entry. To correct, use `update_knowledge`. `knowledge_id` must come from a search hit in this run; `thought` is why it is wrong. A run gets {limit} deletions. Before first use, read the knowledge_base skill.
