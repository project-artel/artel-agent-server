---
version: v41
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Record the verdict for one test case, by its case_id, after carrying out its test_step and watching the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry it out. `passed`: did observed match expected. Not reaching a case is not a verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "". `overwrite=true` replaces a verdict, once.
