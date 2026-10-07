---
version: v18
note: click 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Click `target`; `button` is 0 left, 1 right, 2 middle. `target` is a screen point ("640,360", the element's @ x,y centre exactly as printed), a Unity instance id ("#12345") or a hierarchy path selector ("Root[0]/Canvas[1]/Card(Clone)[3]"). An id or selector is resolved when the action runs, so it still hits an element that moved. Move, press and release go as one batch. A target in none of the three forms is refused before anything reaches the game.
