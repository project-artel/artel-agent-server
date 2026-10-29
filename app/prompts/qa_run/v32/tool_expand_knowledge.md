---
version: v32
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [relations, similar, limit]
---
Follow an entry's relations further than a search showed. `depth` 1 is the neighbours you have, 2 goes one further; larger is clamped. A neighbour marked {relations} was asserted with a note; one marked `{similar}` is a text-similarity guess with no note, so treat it as a hint. `knowledge_id` must be one shown to you this run. A run gets {limit} expansions. Before first use, read the knowledge_base skill.
