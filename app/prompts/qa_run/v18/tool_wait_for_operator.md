---
version: v18
note: wait_for_operator 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Stop and wait until the operator says something, for when you cannot go on without a person. Ask with `reply_to_operator` first, then wait. Returns what they said, or that nobody answered in `timeout_seconds`; silence is not a failure. Waiting is capped per call, and a couple of full waits is the run's clock spent on nothing.
