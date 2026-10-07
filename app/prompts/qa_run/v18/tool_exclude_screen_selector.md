---
version: v18
note: v17 의 EXCLUDE_SCREEN_SELECTOR_DESCRIPTION 을 줄인 것이다.
placeholders: []
---
Tell the content map that this selector does not separate screens in this scene. Call it when the `content map:` line keeps naming new screen ids for a screen that has not visibly changed. It rewrites recorded screens and folds identical ones, and they do not come back. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
