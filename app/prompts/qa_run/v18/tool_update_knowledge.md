---
version: v18
note: v17 의 UPDATE_KNOWLEDGE_DESCRIPTION 을 줄인 것이다.
placeholders: [tags, limit]
---
Correct a knowledge entry that is wrong, keeping its id and history. `knowledge_id` must come from a `search_knowledge` hit in this run. Send only the fields that change: `tag` (one of {tags}), `summary`, `description`; at least one. One disagreement with what you saw is more often a bug than stale knowledge: report it with `report_step` instead. A run gets {limit} writes, shared with `record_knowledge`. Before first use, read the knowledge_base skill.
