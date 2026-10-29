---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Drag from `from_target` to `to_target` and drop there. Each end is a screen point, a Unity instance id or a selector, as in `click`, and may differ in kind. Ids and selectors resolve when each move runs. `button` is 0 left, 1 right, 2 middle. Move, press, move and release go as one batch, so it cannot be left with the button down. An end in none of the forms is refused before anything is sent.
