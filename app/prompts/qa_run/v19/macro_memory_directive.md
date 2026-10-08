---
version: v19
note: phase 를 강제하고 macro 가 켜진 런에만 phase_directive 뒤에 붙는 문단. 통과한 step 에 macro 초안이 붙으면 다음 phase 가 REVIEW_DRAFT 이고 `register_macro` 나 `decline_macro_draft` 로 답한 뒤에야 UPDATE_MEMORY 가 지식을 묻는다는 순서를 적고, 초안이 없던 순서는 UPDATE_MEMORY 에서 직접 써 남기게 한다. 조건은 run_config.macro_memory_directive_applies 하나다.
placeholders: []
---
Macros live inside this order too. `write_macro`, `edit_macro` and `read_macro` ignore it: a draft changes nothing outside this run, so write and fix one whenever you need to. `register_macro` is never refused in another phase: register a macro the moment you have one worth keeping, even mid-ACT. A macro you wrote yourself and register during UPDATE_MEMORY is an answer there — `register_macro` answers UPDATE_MEMORY, the same as a knowledge entry.

When a step's verdict carries a macro draft, the next phase is REVIEW_DRAFT, and it comes before UPDATE_MEMORY asks about knowledge. Answer it with `register_macro`, which is the default for a step that passed — fix the draft with `edit_macro` first if something in it is wrong. Or answer with `decline_macro_draft`, naming the draft and what replaying it in the next run would do wrong. This scenario runs again on later builds, the next run reaches the same steps, and it starts with a list of the macros registered on this build. A draft you do not register is gone when the run ends, and the next run sends those actions by hand again.

So when UPDATE_MEMORY comes, ask two questions, not one: what did this step teach about the game, and did it have a sequence you would do again that no draft covered — the presses for every dialogue line, the combine-and-attack for every enemy? If it did, write that sequence as a macro and register it there. `run_macro` and `resume_macro` belong to ACT, so a macro you register now is yours to run from the next step on.
