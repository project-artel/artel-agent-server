---
version: v30
note: extend_behavior 도구 description (ARTEL-1054). 500자 이하.
placeholders: []
---
Give a running behavior `seconds` more time before it times out. A behavior may run at most 300 seconds from its start in total; the game refuses an extension past that. Use it when the behavior is still doing what the step needs and its time is nearly up — the pulse view's `Behavior running` line shows seconds run and the limit.
