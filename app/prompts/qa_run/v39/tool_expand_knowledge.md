---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: [relations, similar, limit]
---
Follow an entry's relations further than a search showed. `depth` 1 is the neighbours you have, 2 goes one further; larger is clamped. A neighbour marked {relations} was asserted with a note; one marked `{similar}` is a text-similarity guess with no note, so treat it as a hint. `knowledge_id` must be one shown to you this run. A run gets {limit} expansions. Before first use, read the knowledge_base skill.
