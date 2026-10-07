---
version: v19
note: phase 를 강제하고 macro 가 켜진 런에만 phase_directive 뒤에 붙는 문단. 지식화 단계(UPDATE_MEMORY)에서 방금 한 순서를 macro 로 남기게 한다. 조건은 run_config.macro_memory_directive_applies 하나다.
placeholders: []
---
Macros live inside this order too. `write_macro`, `edit_macro` and `read_macro` ignore it: a draft changes nothing outside this run, so write and fix one whenever you need to. `register_macro` answers UPDATE_MEMORY, the same as a knowledge entry — a registered macro is something the next step and the next run can call.

So when UPDATE_MEMORY comes, ask two questions, not one: what did this step teach about the game, and did it have a sequence you would do again — the presses for every dialogue line, the combine-and-attack for every enemy? If it did, write that sequence as a macro and register it there. `run_macro` and `resume_macro` belong to ACT, so a macro you register now is yours to run from the next step on.
