---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Press a key and keep it down. `key_code` is a Unity KeyCode name such as "W", "LeftShift", "Space". For input the game reads as held; `press_key` is the one-shot. Nothing releases it: call `release_key` before you judge the step. Rules for held input: see the held_state skill.
