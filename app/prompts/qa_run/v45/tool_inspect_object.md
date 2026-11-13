---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Read every value the game holds on one object, which the scene block does not list (an enemy's health, a counter, a flag). `selector` is the address printed in the scene block; a partial one matches (`RangedCat` finds `RangedCat(Clone)[17]`).
