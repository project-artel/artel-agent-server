---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Freeze game time so a moving screen (hit effect, countdown, toast) stays still while you read it. Clicking, typing and observing still work. Nothing unfreezes it: call `resume_game_time` before you report the step. Rules for held state: see the held_state skill.
