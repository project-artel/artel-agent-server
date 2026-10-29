---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: [severities, limit]
---
File a defect unrelated to a TC. TC failures reported by `report_test_case` create issues automatically. `severity` is one of {severities}, worst first. `expected` and `actual` say what should and did happen; `reproduction` is the shortest steps, oldest first. You may file {limit} of these in one run. One defect, one call: do not refile the same broken screen.
