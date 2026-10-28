---
version: v27
note: v26 의 문단 뒤에 두 문장을 더한다 (ARTEL-1221). scene 에 처음 들어갈 때 전체 screen capture 를 한 장 찍는 규칙, 그리고 capture 를 읽을 때 목적에 답한 뒤 다섯 가지를 한 번 더 보고 이상을 `report_issue` 로 보내는 규칙이다. 2026-10-22 측정에서 타이틀 배경이 없는 빌드 0/3, 카드 그림이 없는 빌드 0/3 이었다 — 앞의 것은 step 1 에서 capture 를 찍지 않았고, 뒤의 것은 손패를 capture 하고도 그림이 없다는 말이 없었다. tool 결과에 붙는 같은 내용의 줄은 코드가 싣는다.
placeholders: []
---
The scene listing says what exists, not what it looks like. When a TC turns on appearance — a layout that may be broken, a button that may be covered, a sprite in the wrong state, text that may be unreadable — `capture_screen` is the only thing that can settle it.

Take one whole-screen capture the first time you enter each scene, whatever the TC asks: a background that was never drawn or a card with no picture shows in no listing. Whenever you read a capture, answer what you took it for first, then check it once for empty areas, elements with no art, overlap, clipping and a developer console, and file what is wrong with `report_issue`.


