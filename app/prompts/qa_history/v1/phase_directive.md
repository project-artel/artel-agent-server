---
version: v1
note: 이전 검증 기록 조회는 어느 phase에서도 읽기만 수행.
placeholders: []
---
`read_test_case_history` is allowed in every phase and never advances the phase. Its historical records are reference only; verify the current game before reporting a verdict.
