---
version: v43
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
TC verdicts and reusable knowledge are separate records. report_test_case accepts a TC verdict, reason, evidence checks and used_knowledge_ids. Cite only knowledge this run has seen. It does not accept capability_key or learned. Use record_capability_verdict for a capability actually observed, and record_knowledge for useful reusable findings. Do not invent either to complete a TC.
