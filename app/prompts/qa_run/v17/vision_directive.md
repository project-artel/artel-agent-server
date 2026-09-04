---
version: v17
note: v16 그대로 두면 이 문단의 "text that may be unreadable" 사례가, 이미 `the screen reads:` 로 오는 글자를 `capture_screen` 으로 다시 찾으라는 말로 읽힌다 — system.md 쪽에 그 절을 가르치는 문단을 더한 것과 정면으로 부딪힌다. 그래서 이 문단만 고쳐 쓴다. 글자의 내용은 이미 있다는 것을 먼저 말하고, `capture_screen` 이 답하는 것은 내용이 아니라 모양 — 레이아웃이 깨졌는지, 버튼이 가려졌는지, 글자가 읽을 수 있게 그려졌는지 — 뿐이라는 것으로 좁힌다. 씬 문맥 블록과 capability 절은 여전히 무관해 안 건드린다.
placeholders: []
---
The scene listing says what exists, not what it looks like, and neither does the text in it — `the screen reads:` already carries the game's words verbatim, so `capture_screen` is never how you find out what a line says. When a step turns on appearance instead — a layout that may be broken, a button that may be covered, a sprite in the wrong state, text drawn where it may not be legible — `capture_screen` is the only thing that can settle it.


