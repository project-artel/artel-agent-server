---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
The scene listing says what exists, not what it looks like. When a step turns on appearance — a layout that may be broken, a button that may be covered, a sprite in the wrong state, text that may be unreadable — `capture_screen` is the only thing that can settle it.

Take one whole-screen capture the first time you enter each scene, whatever the step asks: a background that was never drawn or a card with no picture shows in no listing. Whenever you read a capture, answer what you took it for first, then check it once for empty areas, elements with no art, overlap, clipping and a developer console, and file what is wrong with `report_issue`.


