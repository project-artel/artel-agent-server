---
version: v37
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: [severities, limit]
---
File a defect in the GAME, not the step verdict (that is `report_step`). `severity` is one of {severities}, worst first. `expected` and `actual` say what should and did happen; `reproduction` is the shortest steps, oldest first. You may file {limit} of these in one run. One defect, one call: do not refile the same broken screen.
