---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Look at the game screen; returns what changed since your last look. `wait_seconds` waits first (loading, animation). `current_scene=True` returns the whole scene, several times larger, so use it only when the ordinary view cannot answer. `step` is the scenario step; `thought` is why you look. Always look before acting.
