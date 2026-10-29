---
version: v36
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Freeze game time so a moving screen (hit effect, countdown, toast) stays still while you read it. Clicking, typing and observing still work. Nothing unfreezes it: call `resume_game_time` before you report the step. Rules for held state: see the held_state skill.
