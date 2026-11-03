---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
List the exceptions the game has logged that you have not read yet — type, message, first stack line, how often and since which frame — and mark them read, so `unseen errors: N` drops to 0 and a later repeat of the same one is not counted again. `include_seen=true` lists every one again. An exception never decides a step's verdict; a real defect goes in with `report_issue`.
