---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
List the exceptions the game has logged that you have not read yet — type, message, first stack line, how often and since which frame — and mark them read, so `unseen errors: N` drops to 0 and a later repeat of the same one is not counted again. `include_seen=true` lists every one again. An exception never decides a step's verdict; a real defect goes in with `report_issue`.
