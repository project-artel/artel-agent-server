---
version: v38
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Click `target`; `button` is 0 left, 1 right, 2 middle. `target` is a screen point ("640,360", the element's @ x,y centre exactly as printed), a Unity instance id ("#12345") or a hierarchy path selector ("Root[0]/Canvas[1]/Card(Clone)[3]"). An id or selector is resolved when the action runs, so it still hits an element that moved. Move, press and release go as one batch. A target in none of the three forms is refused before anything reaches the game.
