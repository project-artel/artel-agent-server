---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [limit]
---
Remove a relation between two knowledge entries; both entries stay. Name it as you saw it: `from_knowledge_id`, `to_knowledge_id`, `relation`. Remove only a connection that was itself wrong, never because the build is broken today: that is a bug for `report_issue`. `thought` is the only record of why it went away. A run gets {limit} unlinks. Before first use, read the knowledge_base skill.
