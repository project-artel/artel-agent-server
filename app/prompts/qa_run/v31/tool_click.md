---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Click `target`; `button` is 0 left, 1 right, 2 middle. `target` is a screen point ("640,360", the element's @ x,y centre exactly as printed), a Unity instance id ("#12345") or a hierarchy path selector ("Root[0]/Canvas[1]/Card(Clone)[3]"). An id or selector is resolved when the action runs, so it still hits an element that moved. Move, press and release go as one batch. A target in none of the three forms is refused before anything reaches the game.
