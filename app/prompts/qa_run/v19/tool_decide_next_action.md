---
version: v19
note: decide_next_action 도구 description 을 코드(`phase_tools.py` 의 상수)에서 옮김. 500자 이하. 이 tool 과 `thought` 의 차이는 `decide_directive` 가 더 적는다.
placeholders: []
---
State what you are about to do for this step, before you do it. One call per step, before the action tool: the step number, the one next action in `plan`, and in `expected` what the screen shows if it worked, the sentence `report_step` will judge against. It changes nothing in the game. Keep it to one action: "open the shop, buy the sword and equip it" is three steps of plan and cannot be verified.
