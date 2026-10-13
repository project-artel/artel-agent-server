---
version: v19
note: drag 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Drag from `from_target` to `to_target` and drop there. Each end is a screen point, a Unity instance id or a selector, as in `click`, and may differ in kind. Ids and selectors resolve when each move runs. `button` is 0 left, 1 right, 2 middle. Move, press, move and release go as one batch, so it cannot be left with the button down. An end in none of the forms is refused before anything is sent.
