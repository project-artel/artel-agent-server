---
version: v22
note: resume_macro 도구 description. 500자 이하. checkpoint 의 규칙은 macro skill 에 있다. v22: Jev 가 맞는 case 를 못 찾아 멈춘 macro 도 이것으로 답한다.
placeholders: []
---
Carry on, or stop, a macro paused at a `checkpoint` or where Jev found no fitting `case`. `proceed: true` runs it on from the line after the checkpoint, with every name it bound still bound; `proceed: false` stops it there and sends nothing else. `step` must be the step the macro was called for. While a macro is paused nothing is sent, and `run_macro` is refused until you answer here. The answer reads like `run_macro`'s, and says whether it paused again. See the macro skill.
