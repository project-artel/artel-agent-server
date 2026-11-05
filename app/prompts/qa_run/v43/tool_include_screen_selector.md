---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Tell the content map that this selector separates screens in this scene. Call it once, when the game plainly shows a different screen but the `content map:` line keeps the same screen id. It does not split screens already merged; it applies from your next observation. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
