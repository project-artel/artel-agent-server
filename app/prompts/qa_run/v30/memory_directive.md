---
version: v30
note: TC 검증 시도별 판정과 참고 step 분리. 사용자 질문 대기와 별도 issue 규칙 반영.
placeholders: []
---
TC verdicts and reusable knowledge are separate records. report_test_case accepts a TC verdict, reason, evidence checks and used_knowledge_ids. Cite only knowledge this run has seen. It does not accept capability_key or learned. Use record_capability_verdict for a capability actually observed, and record_knowledge for useful reusable findings. Do not invent either to complete a TC.
