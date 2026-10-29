---
version: v34
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Summarise the game's performance over a window: average and p95 frame time, lowest fps, hitch count, memory growth and CPU, from the one-second samples the game sends. Give `last_seconds`, or `since_frame` for everything after a frame; with neither you get the last 10 seconds. Use it when a result shows hitches or the game feels slow. A performance problem is filed with `report_issue`; it never decides a step's verdict.
