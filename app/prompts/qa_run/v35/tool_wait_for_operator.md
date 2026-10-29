---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Stop and wait until the operator says something, for when you cannot go on without a person. Ask with `reply_to_operator` first, then wait. Returns what they said, or that nobody answered in `timeout_seconds`; silence is not a failure. Waiting is capped per call, and a couple of full waits is the run's clock spent on nothing.
