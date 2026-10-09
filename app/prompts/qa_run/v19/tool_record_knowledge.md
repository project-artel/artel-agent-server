---
version: v19
note: v17 의 RECORD_KNOWLEDGE_DESCRIPTION 을 줄인 것이다. 무엇이 규칙이고 무엇이 이 런의 상태인지 가르는 설명은 knowledge_base skill 로 옮겼다.
placeholders: [tags, limit]
---
Record a rule about this game that would still be true in tomorrow's run on a fresh save. `tag` is one of {tags}; `summary` is the fact in one sentence; `description` is the condition and what you saw. `scene_name` only if it holds in one place. Not for this run's state, screens and routes, scenario text, or bugs (`report_step`). A run gets {limit} writes, shared with `update_knowledge`. A repeat files it twice. Before first use, read the knowledge_base skill.
