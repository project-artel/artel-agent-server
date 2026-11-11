---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [tags, limit]
---
Correct a knowledge entry that is wrong, keeping its id and history. `knowledge_id` must come from a `search_knowledge` hit in this run. Send only the fields that change: `tag` (one of {tags}), `summary`, `description`; at least one. One disagreement with what you saw is more often a bug than stale knowledge: report it with `report_test_case` instead. A run gets {limit} writes, shared with `record_knowledge`. Before first use, read the knowledge_base skill.
