---
version: v18
note: v17 의 RECORD_NEW_CAPABILITY_DESCRIPTION 을 줄인 것이다. observed 와 inferred 의 구분과 한 줄 규칙은 content_map skill 로 옮겼다.
placeholders: [summary_max, interactions, rationale_max]
---
Add a capability the content map never mentioned, in the scene you stand on. Check `list_scene_capabilities` first. `summary` at most {summary_max} characters; `interaction` is one of {interactions}; `origin` is `observed` (needs `verdict`) or `inferred` (needs `based_on` observation ids). `rationale` at most {rationale_max} characters. A row cannot be edited or deleted. Before first use, read the content_map skill.
