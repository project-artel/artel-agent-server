---
version: v40
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Tell the content map that this selector does not separate screens in this scene. Call it when the `content map:` line keeps naming new screen ids for a screen that has not visibly changed. It rewrites recorded screens and folds identical ones, and they do not come back. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
