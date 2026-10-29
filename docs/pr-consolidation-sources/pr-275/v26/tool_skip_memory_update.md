---
version: v26
note: skip_memory_update 도구 description 을 코드(`phase_tools.py` 의 상수)에서 옮김. 500자 이하.
placeholders: []
---
Say that this TC attempt left nothing worth keeping past the run. Call it instead of `record_knowledge`, `record_capability_verdict` or `record_new_capability` when there is nothing to write. `reason` is one required line saying why, such as "the button did exactly what its label says". An empty `reason` is refused and UPDATE_MEMORY stays open.
