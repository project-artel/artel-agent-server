---
version: v26
note: phase_cycle 이 off 가 아닐 때 FREE 시나리오에 주는 report_case description. v24 에 `performed` 와 message 의 expected/observed 두 줄을 더한다 (ARTEL-1220). 500자 이하.
placeholders: []
---
Record the verdict for one test case, by its case_id, after carrying out its test_step and watching the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry it out. `passed`: did observed match expected. Not reaching a case is not a verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "". `overwrite=true` replaces a verdict, once.
