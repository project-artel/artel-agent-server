---
version: v41
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Freeze game time so a moving screen (hit effect, countdown, toast) stays still while you read it. Clicking, typing and observing still work. Nothing unfreezes it: call `resume_game_time` before you report the step. Rules for held state: see the held_state skill.
