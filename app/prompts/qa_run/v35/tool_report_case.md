---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step with its precondition holding and watched the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the test_step out. `passed`: did the observed result match the expected one. Not having reached a case yet is not a verdict. `used_knowledge_ids`: entries that changed your verdict. `overwrite=true` replaces an earlier verdict, once.
