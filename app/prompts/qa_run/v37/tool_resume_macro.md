---
version: v37
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Carry on, or stop, a macro paused at a `checkpoint` or where Jev found no fitting `case`. `proceed: true` runs it on from the line after the checkpoint, with every name it bound still bound; `proceed: false` stops it there and sends nothing else. `step` must be the step the macro was called for. While a macro is paused nothing is sent, and `run_macro` is refused until you answer here. The answer reads like `run_macro`'s, and says whether it paused again. See the macro skill.
