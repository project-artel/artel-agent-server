---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Let game time run again at the speed it had before `pause_game_time`. Fails if the game was not paused by that tool, because a speed the game chose is not yours to overwrite.
