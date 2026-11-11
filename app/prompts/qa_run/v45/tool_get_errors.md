---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
List the exceptions the game has logged that you have not read yet — type, message, first stack line, how often and since which frame — and mark them read, so `unseen errors: N` drops to 0 and a later repeat of the same one is not counted again. `include_seen=true` lists every one again. An exception never decides a step's verdict; a real defect goes in with `report_issue`.
