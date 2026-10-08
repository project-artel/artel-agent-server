---
version: v19
note: phase 를 강제하고 macro 가 켜진 런에만 phase_directive 뒤에 붙는 문단. 첫 단락이 macro 를 이 런의 기본 조작 방법으로, 개별 action tool 을 macro 가 없거나 실패했을 때 메우는 용도로 정하고 step 하나의 순서(등록된 macro 먼저, 없으면 써서 실행, 한 번 누르기·살펴보기·복구만 손으로)를 적는다(v16). 그 뒤는 v15 와 같다 — 통과한 step 에 macro 초안이 붙으면 다음 phase 가 REVIEW_DRAFT 이고 `register_macro` 나 `decline_macro_draft` 로 답한 뒤에야 UPDATE_MEMORY 가 지식을 묻는다. 조건은 run_config.macro_memory_directive_applies 하나다.
placeholders: []
---
In this run, macros are how you operate the game. The action tools — `press_key`, `click` and the rest — fill in where no macro fits. For each step, in this order:
1. If a registered macro covers the step, run it with `run_macro` as the step's first action. The list at the start of the run names them, and `report_step`'s answer names the one for the next step when there is one.
2. If none does and the step needs more than two or three actions, write it as a macro with `write_macro` and run it, rather than sending the actions one by one. Check the scene first and branch on what is there: `if exists(...)` before a panel you may need to open, a while loop until the screen changes instead of a fixed count.
3. Send by hand only a single action, a look around, or what is left after a macro stopped partway — then fix that macro with `edit_macro`.

Macros live inside the phase order too. `write_macro`, `edit_macro` and `read_macro` ignore it: a draft changes nothing outside this run, so write and fix one whenever you need to. `run_macro` and `resume_macro` belong to ACT, so a macro runs as part of a step's actions, never while UPDATE_MEMORY or REVIEW_DRAFT is waiting for an answer. `register_macro` is never refused in another phase: register a macro the moment you have one worth keeping, even mid-ACT. A macro you wrote yourself and register during UPDATE_MEMORY is an answer there — `register_macro` answers UPDATE_MEMORY, the same as a knowledge entry.

When a step's verdict carries a macro draft, the next phase is REVIEW_DRAFT, and it comes before UPDATE_MEMORY asks about knowledge. Answer it with `register_macro`, which is the default for a step that passed — fix the draft with `edit_macro` first if something in it is wrong. Or answer with `decline_macro_draft`, naming the draft and what replaying it in the next run would do wrong. This scenario runs again on later builds, the next run reaches the same steps, and it starts with a list of the macros registered on this build. A draft you do not register is gone when the run ends, and the next run sends those actions by hand again.

So when UPDATE_MEMORY comes, ask two questions, not one: what did this step teach about the game, and did it have a sequence you would do again that no draft covered? If it did, write that sequence as a macro and register it there.
