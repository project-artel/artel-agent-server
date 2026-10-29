---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Say that this TC attempt left nothing worth keeping past the run. Call it instead of `record_knowledge`, `record_capability_verdict` or `record_new_capability` when there is nothing to write. `reason` is one required line saying why, such as "the button did exactly what its label says". An empty `reason` is refused and UPDATE_MEMORY stays open.
