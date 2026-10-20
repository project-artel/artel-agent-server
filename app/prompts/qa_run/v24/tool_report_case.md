---
version: v24
note: FREE 시나리오에서 report_step 대신 주는 report_case 도구 description (ARTEL-1029). 500자 이하.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step with its precondition holding and watched the result. `passed`: did the expected result occur. Not having reached a case yet is not a verdict. `message` cites what you saw. `used_knowledge_ids`: only entries that changed your verdict. `overwrite=true` replaces an earlier verdict for the case, once.
