---
version: v24
note: phase_cycle 이 off 가 아닐 때 FREE 시나리오에 주는 report_case description (ARTEL-1029). 500자 이하.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step and watched the result. `passed`: did the expected result occur. Not having reached a case yet is not a verdict. `message` cites what you saw. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "". `overwrite=true` replaces an earlier verdict, once.
