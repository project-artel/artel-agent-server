---
version: v37
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Stop and wait until the operator says something, for when you cannot go on without a person. Ask with `reply_to_operator` first, then wait. Returns what they said, or that nobody answered in `timeout_seconds`; silence is not a failure. Waiting is capped per call, and a couple of full waits is the run's clock spent on nothing.
