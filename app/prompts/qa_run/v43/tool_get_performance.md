---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Summarise the game's performance over a window: average and p95 frame time, lowest fps, hitch count, memory growth and CPU, from the one-second samples the game sends. Give `last_seconds`, or `since_frame` for everything after a frame; with neither you get the last 10 seconds. Use it when a result shows hitches or the game feels slow. A performance problem is filed with `report_issue`; it never decides a step's verdict.
