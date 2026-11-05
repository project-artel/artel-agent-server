---
version: v41
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: [tags, limit]
---
Correct a knowledge entry that is wrong, keeping its id and history. `knowledge_id` must come from a `search_knowledge` hit in this run. Send only the fields that change: `tag` (one of {tags}), `summary`, `description`; at least one. One disagreement with what you saw is more often a bug than stale knowledge: report it with `report_test_case` instead. A run gets {limit} writes, shared with `record_knowledge`. Before first use, read the knowledge_base skill.
