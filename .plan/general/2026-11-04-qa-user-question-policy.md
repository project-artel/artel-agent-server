# 2026-11-04 — QA 사용자 질문의 workflow 변경 요청 제한

- Date: 2026-11-04
- Jira: ARTEL-1375
- Status: Implemented

## Goal
QA 질문을 게임 의도·기대 결과·사람의 실제 작업 확인으로 제한하고 agent 구현 변경 요청으로 QA가 중단되는 문제를 줄인다.

## Non-goals
게임 knowledge에 agent 내부 구현을 기록하지 않는다. 서버 schema·tool 인자·pause/resume 계약·질문 분류 API·운영 배포는 변경하지 않는다.

## Context / Constraints
QA12는 capture_screen의 ACT 거절 후 실행 단계 제한 수정을 요청했다. ACT 관찰 수정 PR302는 이미 develop에 merge되어 있다. 로컬 실행 중인 구버전은 별도로 갱신해야 한다. 질문 description과 runner의 공통 보고 계약이 진행 방법을 모르면 질문하도록 넓게 지시한다. Hindsight 연결 불가.

## Approach (Checklist)
- [x] **Step 0: Recon** 실제 질문, 실행 코드와 merged ACT 수정 확인.
- [x] **Step 1: Implementation** 공통 질문 정책을 system prompt 최종 reporting contract·TC plan·ask_user description에 적용하며 앞선 prompt의 넓은 질문 지시를 대체한다고 명시한다. 게임 기준·사람 작업 확인은 허용하며 agent 코드·workflow·tool 제한·인프라 수정 요청은 금지한다. 거절·오류 시 문서화된 도구와 TC 원문을 확인하고 허용된 대안을 시도한 뒤 증거 부족은 INCONCLUSIVE로 보고한다. keyword 차단은 추가하지 않는다. QA_ARCH_LABEL과 구조 고정 테스트를 갱신한다.
- [x] **Step 2: Tests** 실제 create_agent로 조립되는 system prompt와 plan에 정책이 전달되는지 검증한다. tool schema·기존 질문 대기와 사용자 답변 재개를 유지하고 INCONCLUSIVE 보고·ACT 관찰 회귀를 실행한다. 전체 pytest와 compile 검증. 실제 QA는 origin/develop baseline90ec407과 변경본을 같은 단일 TC scenario·게임 build·gpt/gpt-6.1-sol·prompt v42로 각1회 비교한다. model/prompt_version/agent_arch/agent_fingerprint와 통과/전체 수를 기록한다. QA12는 FAILED로 종료됐다. 실행 중인 QA가 있으면 기다리며 중단하지 않는다. 별도 로컬 검증용 TC/scenario/TR만 만들고 사용자 기존 TC/TR은 편집하지 않는다.
- [x] **Step 3: Rollout / Rollback** draft PR. 로컬 agent는 실행 중인 QA를 끊지 않고 idle 때 반영한다. 변경 commit revert로 정책만 되돌린다.

## Validation
- **Commands to run:** 기존 WSL venv의 python -m pytest와 compileall. 집중 테스트는 test_qa_case_evaluation.py, test_qa_phase_cycle.py, test_qa_arch.py, prompt 조립 회귀.
- **Expected output:** 허용 질문의 pause/resume·schema 유지, 모든 prompt 버전 실행에 공통 정책 적용, ACT 관찰 허용 유지.

## Risks & Rollback
- **Risks:** 자연어 지시는 질문 내용을 서버에서 강제로 분류하는 보장이 아니다. 실제 모델은 sampling되므로 테스트·실행 한 번으로 발생률 개선을 주장하지 않는다. 로컬 실행 중 QA의 질문·답변을 자동 처리하지 않는다.
- **Rollback steps:** 정책 commit revert. 기존 질문·판정·게임 knowledge는 보존한다.

## Open Questions
- 실행 중 QA를 중단하지 않는 로컬 반영 시점.

## 결과
- Fast/Medium/Heavy 계획 검토와 최종 pair review PASS.
- 압축 후 authoritative ledger에 남은 넓은 질문 지시도 공통 정책으로 교체했다.
- 전체 suite3316 PASS, 질문·도구·phase·구조 집중246 PASS, 최종 prompt/compaction67 PASS.
- 실제 동일 단일TC 비교: baseline QA14 1/1, 변경후 QA15 1/1, 각각1회·질문0건. model/prompt/build 동일. 발생률 개선은 주장하지 않는다.
- 로컬 agent8000은 변경본을 사용한다. 기존 QA12 종료를 확인한 뒤 전환했다.
