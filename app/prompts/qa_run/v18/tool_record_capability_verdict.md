---
version: v18
note: v17 의 RECORD_CAPABILITY_VERDICT_DESCRIPTION 을 줄인 것이다. 472 중 2 행만 확인됐다는 실측과 쓸 때를 가리는 설명은 content_map skill 로 옮겼다.
placeholders: [rationale_max]
---
Say that a content map capability worked or failed because you watched it. Name the row with `capability_key` (the bracketed value on its line), or `capability_id` for a row you just created; send exactly one. `verdict` is `works` or `fails`. `rationale` is what you saw, identifiers included, at most {rationale_max} characters. Optional `action_method`. Only the scene you stand on. Not a bug report. Before first use, read the content_map skill.
