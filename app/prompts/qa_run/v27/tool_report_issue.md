---
version: v27
note: report_issue 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: [severities, limit]
---
File a defect unrelated to a TC. TC failures reported by `report_test_case` create issues automatically. `severity` is one of {severities}, worst first. `expected` and `actual` say what should and did happen; `reproduction` is the shortest steps, oldest first. You may file {limit} of these in one run. One defect, one call: do not refile the same broken screen.
