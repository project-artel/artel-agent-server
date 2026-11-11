---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
DEPRECATED, use `click`. Invokes a Button's onClick directly by `target_id` (an id from the scene you just saw). It skips the pointer and the EventSystem, so it ignores occlusion and reaches only a Button. `step` is the scenario step; `thought` is why you click.
