---
version: v39
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
TC verdicts and reusable knowledge are separate records. report_test_case accepts a TC verdict, reason, evidence checks and used_knowledge_ids. Cite only knowledge this run has seen. It does not accept capability_key or learned. Use record_capability_verdict for a capability actually observed, and record_knowledge for useful reusable findings. Do not invent either to complete a TC.
