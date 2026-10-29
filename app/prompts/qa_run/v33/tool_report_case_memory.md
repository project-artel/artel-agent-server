---
version: v33
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step and watched the result. `passed`: did the expected result occur. Not having reached a case yet is not a verdict. `message` cites what you saw. `capability_key`: the content map row checked, from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "". `overwrite=true` replaces an earlier verdict, once.
