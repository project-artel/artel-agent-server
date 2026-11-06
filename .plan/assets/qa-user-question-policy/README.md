# QA 사용자 질문 정책 검증

2026-11-04, ARTEL-1375.

## 자동 검증

- 기존 전체 suite: 3,316 PASS.
- 질문 정책·도구·ACT 관찰 집중 검증: 246 PASS.
- 최종 압축 후 질문 정책을 포함한 prompt·compaction 경로: 67 PASS.
- 변경한 tool은 description만 수정했다. ask_user의 question/thought 인자, 대기와 답변 재개 계약, 기본 fingerprint는 유지한다.
- create_agent에 실제 전달되는 최종 system prompt를 v1·v42·최신 판본으로 확인한다. TC plan과 압축 후 authoritative ledger도 동일한 정책을 전달한다.

## 실제 로컬 게임 비교

동일 WordVenture build4(v1.5.0), 검증용 단일 TC23/scenario4/TR4, gpt/gpt-6.1-sol·prompt v42로 각1회 실행했다. 기존 TC/TR과 게임 저장 데이터는 편집하지 않았다. 포인터 이동 후 screen capture로 현재 배경·UI가 실제로 표시되는지만 확인한다. 지식·content map은 off, 실행 상한은120초다.

| 항목 | baseline | 변경 후 |
| --- | --- | --- |
| QA run | 14 | 15 |
| 결과 | COMPLETED | COMPLETED |
| TC 통과/전체 | 1/1 | 1/1 |
| 사용자 질문 | 0 | 0 |
| model | gpt/gpt-6.1-sol | gpt/gpt-6.1-sol |
| prompt_version | v42 | v42 |
| agent_arch | v51-question-policy-probe | v52-question-policy-probe |

정확한 fingerprint·전체 arch·tool 목록은 comparison.json에 보존했다. description과 공통 실행 지시는 fingerprint에 포함되지 않으므로 label을 바꿔 구분한다. 변경 후 실행의 git_sha는 로컬 검증 당시 ARTEL-1375-working이며 이후 commit한 이 변경의 working tree를 사용했다.

이 비교는 ACT 관찰을 유지한 상태에서 새 정책으로 실제 게임 QA가 끝나는지 확인하는 smoke test다. baseline도 기존 PR302가 적용된 develop90ec407이므로 ACT 거절을 재현하지 않았다. 잘못된 질문이 두 실행 모두0건이라 질문 발생률 개선이나 제거를 입증하지 않는다. 각1회 결과이므로 성능 개선을 주장하지 않는다.

## 적용 범위

질문 정책은 자연어 system/tool 지시다. 서버에서 질문 의미를 강제로 분류하거나 차단하는 기능은 추가하지 않았다. 게임 의도·기대 결과 확인과 도구로 수행할 수 없는 사람의 게임 내 작업은 계속 질문할 수 있다. tool 거절·오류는 문서와 허용 대안을 확인한 뒤 증거 부족을 INCONCLUSIVE로 보고하도록 지시한다.
