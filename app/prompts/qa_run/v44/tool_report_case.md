---
version: v44
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step with its precondition holding and watched the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the test_step out. `passed`: did the observed result match the expected one. Not having reached a case yet is not a verdict. `used_knowledge_ids`: entries that changed your verdict. `overwrite=true` replaces an earlier verdict, once.
