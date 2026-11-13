---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Record the verdict for one scenario step, once, right after observing its result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the action out. `passed`: did the observed result match the expected one. `used_knowledge_ids`: entries that changed your verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "".
