---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Call a registered macro against the screen you are on now. `arguments` maps the entry `def`'s parameter names to values; every parameter without a default has to be there, and an `object` one takes a selector, never a coordinate or `#id`. A draft is not callable. With both a draft and a registration, the REGISTERED one runs. The answer says what reached the game, what did not, what an `if` skipped, anything flagged — or PAUSED at a `checkpoint`, answered with `resume_macro`. See the macro skill.
