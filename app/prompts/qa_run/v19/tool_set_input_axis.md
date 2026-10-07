---
version: v19
note: set_input_axis 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Drive a named Unity Input Manager axis, for a game that reads axes rather than keys. `axis_name` is case sensitive ("Horizontal"); `value` runs from -1 to 1, 0 is centred; out of range or an unknown axis is refused. Use it when `hold_key` does nothing. Nothing centres it: call it again with 0 before you judge the step. Rules for held input: see the held_state skill.
