---
version: v27
note: stop_behavior 도구 description (ARTEL-1054). 500자 이하.
placeholders: []
---
Stop a running behavior by its `behavior_id`, or every running behavior with `all: true` — exactly one of the two. Stopping is always allowed, in any step. Stop a behavior before you take over the controls it is using, and before a step that verifies those controls. The answer lists the ids that stopped.
