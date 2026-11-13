---
version: v45
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: [severities, limit]
---
File a defect unrelated to a TC. TC failures reported by `report_test_case` create issues automatically. `severity` is one of {severities}, worst first. `expected` and `actual` say what should and did happen; `reproduction` is the shortest steps, oldest first. You may file {limit} of these in one run. One defect, one call: do not refile the same broken screen.
