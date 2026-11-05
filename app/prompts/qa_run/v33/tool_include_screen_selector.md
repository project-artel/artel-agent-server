---
version: v33
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Tell the content map that this selector separates screens in this scene. Call it once, when the game plainly shows a different screen but the `content map:` line keeps the same screen id. It does not split screens already merged; it applies from your next observation. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
