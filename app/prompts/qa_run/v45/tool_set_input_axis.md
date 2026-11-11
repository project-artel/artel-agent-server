---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Drive a named Unity Input Manager axis, for a game that reads axes rather than keys. `axis_name` is case sensitive ("Horizontal"); `value` runs from -1 to 1, 0 is centred; out of range or an unknown axis is refused. Use it when `hold_key` does nothing. Nothing centres it: call it again with 0 before you judge the step. Rules for held input: see the held_state skill.
