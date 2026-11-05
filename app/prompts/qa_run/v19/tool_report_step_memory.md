---
version: v19
note: phase_cycle 이 off 가 아닐 때 쓰는 report_step description. 코드의 `REPORT_STEP_MEMORY_DESCRIPTION` 에서 옮김. `tool_report_step.md` 에 덧붙이지 않고 따로 완결된 문장으로 쓴다 — 붙이면 500자를 넘는다. `capability_key` 와 `learned` 의 긴 설명은 `memory_directive` 가 system prompt 에 싣는다.
placeholders: []
---
Record the verdict for one scenario step, once per step, right after observing its result. `passed`: did the expected result occur, or for a non-verifying step, did you carry the action out. `message` cites what you saw. `used_knowledge_ids`: only entries that changed your verdict. `capability_key`: the content map row this step checked, copied from the square brackets of a line you were shown. `learned` is required: one line a later run should not relearn, or "" for nothing.
