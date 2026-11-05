---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Say that this step left nothing worth keeping past the run. Call it instead of `record_knowledge`, `record_capability_verdict` or `record_new_capability` when there is nothing to write; most steps leave nothing, and that is a real answer. `reason` is one required line saying why, such as "the button did exactly what its label says". An empty `reason` is refused and the step's UPDATE_MEMORY stays open.
