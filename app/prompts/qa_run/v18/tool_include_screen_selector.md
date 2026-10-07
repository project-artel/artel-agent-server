---
version: v18
note: v17 의 INCLUDE_SCREEN_SELECTOR_DESCRIPTION 을 줄인 것이다. 쓸 때를 가리는 기준과 이미 합쳐진 화면을 못 되돌린다는 설명은 content_map skill 로 옮겼다.
placeholders: []
---
Tell the content map that this selector separates screens in this scene. Call it once, when the game plainly shows a different screen but the `content map:` line keeps the same screen id. It does not split screens already merged; it applies from your next observation. `pattern` is an exact string, never a regular expression, copied from the scene view. `match` is `selector`, `path` (sibling indices stripped) or `subtree`. `reason` is required. Before first use, read the content_map skill.
