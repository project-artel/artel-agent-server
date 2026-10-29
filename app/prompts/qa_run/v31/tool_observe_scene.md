---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Look at the game screen; returns what changed since your last look. `wait_seconds` waits first (loading, animation). `current_scene=True` returns the whole scene, several times larger, so use it only when the ordinary view cannot answer. `step` is the scenario step; `thought` is why you look. Always look before acting.
