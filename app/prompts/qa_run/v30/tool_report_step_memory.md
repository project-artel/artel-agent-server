---
version: v30
note: phase_cycle 이 off 가 아닐 때 쓰는 report_step description. v24 에서 `passed` 의 정의를 바꾸고 `performed` 를 더한다 (ARTEL-1220). 500자 이하라 `used_knowledge_ids` 설명을 줄였다.
placeholders: []
---
Record the verdict for one scenario step, once, right after observing its result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the action out. `passed`: did the observed result match the expected one. `used_knowledge_ids`: entries that changed your verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "".
