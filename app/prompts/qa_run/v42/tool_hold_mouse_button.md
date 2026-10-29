---
version: v42
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Press a mouse button (`button`: 0 left, 1 right, 2 middle) and keep it down at the current pointer position; move there first with `move_pointer`. For input the game reads as held. Nothing releases it: call `release_mouse_button` before you judge the step. Rules for held input: see the held_state skill.
