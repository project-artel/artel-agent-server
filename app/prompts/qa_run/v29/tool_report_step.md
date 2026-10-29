---
version: v29
note: v24 의 report_step description 을 ARTEL-1220 에 맞게 다시 쓴다. `passed` 는 기대값과 관찰값이 맞았는가 하나만 뜻하고, 행동을 했는지는 새 인자 `performed` 가 받는다. v24 는 검증 step 이 아니면 `passed` 를 "행동을 했는가" 로 정의해 거부된 행동이 통과로 보고됐다. 500자 이하.
placeholders: []
---
Record the verdict for one scenario step, once, right after observing its result. Begin `message` with `expected:` and `observed:` lines. `performed`: did you carry the action out. `passed`: did the observed result match the expected one — an action the game refused fails a step that expected it to work. `thought` says how you got there. `used_knowledge_ids` lists only entries that changed your verdict, as printed to you; leave it empty otherwise.
