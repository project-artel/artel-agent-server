---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
When `decide_next_action` is in your tool list, call it once per step, before the action tool that carries the step out. Say the step number, the one thing you are about to do in `plan`, and in `expected` what you will see on screen if it worked — the same sentence `report_step` will judge the step against afterwards.

This is not the `thought` every tool already takes. `thought` is why you are doing this, one line, written on the call that actually does something. `decide_next_action` changes nothing in the game or on the screen; it exists so the plan and the expected result are on record before you act, rather than written afterwards to match whatever you happened to see. Keep it to the one next action — a plan spanning several steps cannot be checked against a single `report_step`.


