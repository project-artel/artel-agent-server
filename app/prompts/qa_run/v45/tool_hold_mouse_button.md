---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Press a mouse button (`button`: 0 left, 1 right, 2 middle) and keep it down at the current pointer position; move there first with `move_pointer`. For input the game reads as held. Nothing releases it: call `release_mouse_button` before you judge the step. Rules for held input: see the held_state skill.
