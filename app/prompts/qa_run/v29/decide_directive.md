---
version: v29
note: TC 검증 시도별 판정과 참고 step 분리. 사용자 질문 대기와 별도 issue 규칙 반영.
placeholders: []
---
When decide_next_action is available, record the next action and the expected screen change before acting. Its step argument locates the reference procedure; it does not require a step verdict. report_test_case evaluates the TC expectation after the complete verification attempt.
