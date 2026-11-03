---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [tags, limit]
---
Correct a knowledge entry that is wrong, keeping its id and history. `knowledge_id` must come from a `search_knowledge` hit in this run. Send only the fields that change: `tag` (one of {tags}), `summary`, `description`; at least one. One disagreement with what you saw is more often a bug than stale knowledge: report it with `report_step` instead. A run gets {limit} writes, shared with `record_knowledge`. Before first use, read the knowledge_base skill.
