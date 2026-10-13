---
version: v19
note: hold_key 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Press a key and keep it down. `key_code` is a Unity KeyCode name such as "W", "LeftShift", "Space". For input the game reads as held; `press_key` is the one-shot. Nothing releases it: call `release_key` before you judge the step. Rules for held input: see the held_state skill.
