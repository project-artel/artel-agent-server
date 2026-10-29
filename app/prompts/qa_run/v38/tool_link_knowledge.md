---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: [relations, limit]
---
Record that two knowledge entries are related. `relation` is one of {relations}; `from` is the narrower or dependent one. `note` is required: what you saw. Both ids must have been shown to you this run, as a hit or a neighbour line. If no relation fits, do not link; a vague link crowds out real ones. A run gets {limit} links; a repeat is refused. Before first use, read the knowledge_base skill.
