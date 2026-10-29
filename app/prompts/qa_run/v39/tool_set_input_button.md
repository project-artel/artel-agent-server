---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Hold (`pressed=True`) or release (`pressed=False`) a named input button, for a game that reads GetButton("Jump") by name. `axis_name` is case sensitive; an unknown one is an error. Nothing releases it: call it with pressed=False before you judge the step. Rules for held input: see the held_state skill.
