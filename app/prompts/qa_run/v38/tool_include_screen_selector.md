---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Tell the content map that this selector separates screens in this scene. Call it once, when the game plainly shows a different screen but the `content map:` line keeps the same screen id. It does not split screens already merged; it applies from your next observation. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
