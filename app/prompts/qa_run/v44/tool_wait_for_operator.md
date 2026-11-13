---
version: v44
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Stop and wait until the operator says something, for when you cannot go on without a person. Ask with `reply_to_operator` first, then wait. Returns what they said, or that nobody answered in `timeout_seconds`; silence is not a failure. Waiting is capped per call, and a couple of full waits is the run's clock spent on nothing.
