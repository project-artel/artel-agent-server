---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Stop a running behavior by its `behavior_id`, or every running behavior with `all: true` — exactly one of the two. Stopping is always allowed, in any step. Stop a behavior before you take over the controls it is using, and before a step that verifies those controls. The answer lists the ids that stopped.
