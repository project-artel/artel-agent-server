---
version: v44
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Record the verdict for one test case, by its case_id, after carrying out its test_step and watching the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry it out. `passed`: did observed match expected. Not reaching a case is not a verdict. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "". `overwrite=true` replaces a verdict, once.
