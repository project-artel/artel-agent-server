---
version: v42
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Press a key and keep it down. `key_code` is a Unity KeyCode name such as "W", "LeftShift", "Space". For input the game reads as held; `press_key` is the one-shot. Nothing releases it: call `release_key` before you judge the step. Rules for held input: see the held_state skill.
