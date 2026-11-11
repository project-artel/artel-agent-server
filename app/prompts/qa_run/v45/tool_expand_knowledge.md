---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [relations, similar, limit]
---
Follow an entry's relations further than a search showed. `depth` 1 is the neighbours you have, 2 goes one further; larger is clamped. A neighbour marked {relations} was asserted with a note; one marked `{similar}` is a text-similarity guess with no note, so treat it as a hint. `knowledge_id` must be one shown to you this run. A run gets {limit} expansions. Before first use, read the knowledge_base skill.
