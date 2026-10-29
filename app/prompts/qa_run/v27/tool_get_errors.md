---
version: v27
note: 새 tool `get_errors` 의 description (ARTEL-1219). tool 결과 끝의 `unseen errors: N` 줄이 세는 항목을 펼쳐 보이고 본 것으로 표시한다. 500자 이하.
placeholders: []
---
List the exceptions the game has logged that you have not read yet — type, message, first stack line, how often and since which frame — and mark them read, so `unseen errors: N` drops to 0 and a later repeat of the same one is not counted again. `include_seen=true` lists every one again. An exception never decides a step's verdict; a real defect goes in with `report_issue`.
