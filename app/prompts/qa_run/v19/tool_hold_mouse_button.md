---
version: v19
note: hold_mouse_button 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Press a mouse button (`button`: 0 left, 1 right, 2 middle) and keep it down at the current pointer position; move there first with `move_pointer`. For input the game reads as held. Nothing releases it: call `release_mouse_button` before you judge the step. Rules for held input: see the held_state skill.
