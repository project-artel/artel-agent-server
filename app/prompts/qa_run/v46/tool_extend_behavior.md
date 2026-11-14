---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Give a running behavior `seconds` more time before it times out. A behavior may run at most 300 seconds from its start in total; the game refuses an extension past that. Use it when the behavior is still doing what the step needs and its time is nearly up — the pulse view's `Behavior running` line shows seconds run and the limit.
