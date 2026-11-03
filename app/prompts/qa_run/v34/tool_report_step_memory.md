---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Record the verdict for one scenario step, once per step, right after observing its result. `passed`: did the expected result occur, or for a non-verifying step, did you carry the action out. `message` cites what you saw. `used_knowledge_ids`: only entries that changed your verdict. `capability_key`: the content map row this step checked, copied from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "" for nothing.
