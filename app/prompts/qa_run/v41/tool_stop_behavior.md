---
version: v41
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Stop a running behavior by its `behavior_id`, or every running behavior with `all: true` — exactly one of the two. Stopping is always allowed, in any step. Stop a behavior before you take over the controls it is using, and before a step that verifies those controls. The answer lists the ids that stopped.
