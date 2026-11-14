---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
When decide_next_action is available, record the next action and the expected screen change before acting. Its step argument locates the reference procedure; it does not require a step verdict. report_test_case evaluates the TC expectation after the complete verification attempt.
