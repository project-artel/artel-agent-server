---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Say that this TC attempt left nothing worth keeping past the run. Call it instead of `record_knowledge`, `record_capability_verdict` or `record_new_capability` when there is nothing to write. `reason` is one required line saying why, such as "the button did exactly what its label says". An empty `reason` is refused and UPDATE_MEMORY stays open.
