---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Drive a named Unity Input Manager axis, for a game that reads axes rather than keys. `axis_name` is case sensitive ("Horizontal"); `value` runs from -1 to 1, 0 is centred; out of range or an unknown axis is refused. Use it when `hold_key` does nothing. Nothing centres it: call it again with 0 before you judge the step. Rules for held input: see the held_state skill.
