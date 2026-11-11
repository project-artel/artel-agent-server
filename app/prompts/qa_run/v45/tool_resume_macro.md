---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Carry on, or stop, a macro paused at a `checkpoint` or where Jev found no fitting `case`. `proceed: true` runs it on from the line after the checkpoint, with every name it bound still bound; `proceed: false` stops it there and sends nothing else. `step` must be the step the macro was called for. While a macro is paused nothing is sent, and `run_macro` is refused until you answer here. The answer reads like `run_macro`'s, and says whether it paused again. See the macro skill.
