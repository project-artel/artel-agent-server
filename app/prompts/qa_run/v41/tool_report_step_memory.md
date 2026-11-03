---
version: v41
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Record the verdict for one scenario step, once, right after observing its result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the action out. `passed`: did the observed result match the expected one. `used_knowledge_ids`: entries that changed your verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "".
