---
version: v36
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Hold (`pressed=True`) or release (`pressed=False`) a named input button, for a game that reads GetButton("Jump") by name. `axis_name` is case sensitive; an unknown one is an error. Nothing releases it: call it with pressed=False before you judge the step. Rules for held input: see the held_state skill.
