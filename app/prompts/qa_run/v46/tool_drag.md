---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Drag from `from_target` to `to_target` and drop there. Each end is a screen point, a Unity instance id or a selector, as in `click`, and may differ in kind. Ids and selectors resolve when each move runs. `button` is 0 left, 1 right, 2 middle. Move, press, move and release go as one batch, so it cannot be left with the button down. An end in none of the forms is refused before anything is sent.
