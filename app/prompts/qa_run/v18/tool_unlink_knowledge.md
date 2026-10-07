---
version: v18
note: v17 의 UNLINK_KNOWLEDGE_DESCRIPTION 을 줄인 것이다.
placeholders: [limit]
---
Remove a relation between two knowledge entries; both entries stay. Name it as you saw it: `from_knowledge_id`, `to_knowledge_id`, `relation`. Remove only a connection that was itself wrong, never because the build is broken today: that is a bug for `report_issue`. `thought` is the only record of why it went away. A run gets {limit} unlinks. Before first use, read the knowledge_base skill.
