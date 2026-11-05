---
version: v40
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Press a mouse button (`button`: 0 left, 1 right, 2 middle) and keep it down at the current pointer position; move there first with `move_pointer`. For input the game reads as held. Nothing releases it: call `release_mouse_button` before you judge the step. Rules for held input: see the held_state skill.
