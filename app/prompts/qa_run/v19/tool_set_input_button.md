---
version: v19
note: set_input_button 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Hold (`pressed=True`) or release (`pressed=False`) a named input button, for a game that reads GetButton("Jump") by name. `axis_name` is case sensitive; an unknown one is an error. Nothing releases it: call it with pressed=False before you judge the step. Rules for held input: see the held_state skill.
