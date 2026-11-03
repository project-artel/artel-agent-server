---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Record the verdict for one scenario step, once, right after observing its result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the action out. `passed`: did the observed result match the expected one — an action the game refused fails a step that expected it to work. `thought` says how you got there. `used_knowledge_ids` lists only entries that changed your verdict, as printed to you; leave it empty otherwise.
