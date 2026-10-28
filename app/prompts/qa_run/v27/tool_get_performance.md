---
version: v27
note: 새 tool `get_performance` 의 description (ARTEL-1223). orchestration 이 저장한 `PERFORMANCE` 1초 묶음을 창 하나로 요약한다. tool 결과 끝의 hitch 한 줄이 가리키는 상세다. 500자 이하.
placeholders: []
---
Summarise the game's performance over a window: average and p95 frame time, lowest fps, hitch count, memory growth and CPU, from the one-second samples the game sends. Give `last_seconds`, or `since_frame` for everything after a frame; with neither you get the last 10 seconds. Use it when a result shows hitches or the game feels slow. A performance problem is filed with `report_issue`; it never decides a step's verdict.
