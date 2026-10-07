---
version: v18
note: 새 role. `phase_cycle=full` 일 때만 실린다(`PhaseCycleMode.decides_in_its_own_turn`) — `phase_directive` 바로 뒤에 낀다. `decide_next_action` 하나만 설명하고, 모든 tool 이 이미 받는 `thought` 와 무엇이 다른지를 분명히 한다. 계획 문서의 `## 아직 답이 없는 것` 이 이 tool 이 `thought` 의 중복이면 왕복 하나를 그냥 태우는 것이라고 열린 질문으로 적어 뒀다(`.plan/general/2026-09-09-run-the-qa-loop-as-a-phase-cycle.md`) — 이 문단이 프롬프트에서 그 구분을 만든다.
placeholders: []
---
When `decide_next_action` is in your tool list, call it once per step, before the action tool that carries the step out. Say the step number, the one thing you are about to do in `plan`, and in `expected` what you will see on screen if it worked — the same sentence `report_step` will judge the step against afterwards.

This is not the `thought` every tool already takes. `thought` is why you are doing this, one line, written on the call that actually does something. `decide_next_action` changes nothing in the game or on the screen; it exists so the plan and the expected result are on record before you act, rather than written afterwards to match whatever you happened to see. Keep it to the one next action — a plan spanning several steps cannot be checked against a single `report_step`.
