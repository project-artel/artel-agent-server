---
version: v19
note: run_macro 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Call a registered macro against the screen you are on now. `arguments` maps the entry `def`'s parameter names to values; every parameter without a default has to be there, and an `object` one takes a selector, never a coordinate or `#id`. A draft is not callable. With both a draft and a registration, the REGISTERED one runs. The answer says what reached the game, what did not, what an `if` skipped, anything flagged — or PAUSED at a `checkpoint`, answered with `resume_macro`. See the macro skill.
