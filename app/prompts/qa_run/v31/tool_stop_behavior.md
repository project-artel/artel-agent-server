---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Stop a running behavior by its `behavior_id`, or every running behavior with `all: true` — exactly one of the two. Stopping is always allowed, in any step. Stop a behavior before you take over the controls it is using, and before a step that verifies those controls. The answer lists the ids that stopped.
