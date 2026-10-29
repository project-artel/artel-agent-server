---
version: v33
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [relations, limit]
---
Record that two knowledge entries are related. `relation` is one of {relations}; `from` is the narrower or dependent one. `note` is required: what you saw. Both ids must have been shown to you this run, as a hit or a neighbour line. If no relation fits, do not link; a vague link crowds out real ones. A run gets {limit} links; a repeat is refused. Before first use, read the knowledge_base skill.
