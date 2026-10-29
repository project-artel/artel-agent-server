---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step with its precondition holding and watched the result. `passed`: did the expected result occur. Not having reached a case yet is not a verdict. `message` cites what you saw. `used_knowledge_ids`: only entries that changed your verdict. `overwrite=true` replaces an earlier verdict for the case, once.
