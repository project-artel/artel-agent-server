---
version: v30
note: FREE 시나리오의 report_case description. v24 에 `performed` 와 message 의 expected/observed 두 줄을 더한다 (ARTEL-1220). 500자 이하.
placeholders: []
---
Record the verdict for one test case, by its case_id, after you carried out its test_step with its precondition holding and watched the result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the test_step out. `passed`: did the observed result match the expected one. Not having reached a case yet is not a verdict. `used_knowledge_ids`: entries that changed your verdict. `overwrite=true` replaces an earlier verdict, once.
