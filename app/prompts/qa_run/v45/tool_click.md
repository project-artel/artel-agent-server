---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Click `target`; `button` is 0 left, 1 right, 2 middle. `target` is a screen point ("640,360", the element's @ x,y centre exactly as printed), a Unity instance id ("#12345") or a hierarchy path selector ("Root[0]/Canvas[1]/Card(Clone)[3]"). An id or selector is resolved when the action runs, so it still hits an element that moved. Move, press and release go as one batch. A target in none of the three forms is refused before anything reaches the game.
