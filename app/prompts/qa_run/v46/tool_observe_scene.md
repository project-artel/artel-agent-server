---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Look at the game screen; returns what changed since your last look. `wait_seconds` waits first (loading, animation). `current_scene=True` returns the whole scene, several times larger, so use it only when the ordinary view cannot answer. `step` is the scenario step; `thought` is why you look. Always look before acting.
