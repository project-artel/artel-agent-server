---
version: v19
note: observe_scene 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Look at the game screen; returns what changed since your last look. `wait_seconds` waits first (loading, animation). `current_scene=True` returns the whole scene, several times larger, so use it only when the ordinary view cannot answer. `step` is the scenario step; `thought` is why you look. Always look before acting.
