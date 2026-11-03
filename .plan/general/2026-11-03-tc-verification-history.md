# 2026-11-03 — 동일 TC 이전 검증 기록 조회 추가

- Date: 2026-11-03
- Jira: ARTEL-1350 (orchestration ARTEL-1351 → agent ARTEL-1352)
- Status: 구현·자동 검증 완료, 실게임 전후 비교 미실행

## Goal

QA agent가 같은 프로젝트·TC의 이전 실행에서 수행한 조작, 판정 근거, 사람이 알려준 내용을 조회한다.

## Non-goals

자동 지식 생성, 이전 PASS의 재사용, UI 변경, 배포는 하지 않는다.

## Context / Constraints

기존 작업 변경을 보존하고 최신 develop의 별도 worktree를 쓴다. `qa_case_attempt`를 우선 조회하고 구형 STATUS `case_id`도 지원한다. 시도 시작 때 저장한 snapshot과 legacy backfill snapshot을 구분한다. `qa_operator_question`의 사람 답변과 일반 CHAT을 함께 조회하며 실행 전체의 참고 자료로 표시한다. 사람의 TC 판정 수정은 조회 기준 시각 전에 저장된 revision만 제공한다. 기록이 없는 과거 실행의 명세를 현재 시나리오로 대체하지 않는다.

## Approach (Checklist)
- [x] **Step 0: Recon** — agent tool 조립, run-scoped chat, 영구 qa_log, 내부 API 경계 확인.
- [x] **Step 1: Implementation** — orchestration에 현재 try·프로젝트·TC·knowledge scope를 검증하는 내부 history API 추가. 종료된 이전 try만 조회하고 build·시각·TC step을 제공한다. 최근 실행 목록과 시간순 로그를 각각 cursor로 조회하며 CHAT 중계 사본과 PULSE는 제외한다. 큰 payload는 잘림을 명시한다. agent에 `read_test_case_history` tool을 추가하고 현재 시나리오의 TC만 허용한다. 읽기는 phase를 진행시키지 않으며 오류는 기록 없음과 구분한다. 기존 prompt 판본을 수정하지 않고 새 tool description 판본을 추가한다. arch label과 tool 목록 테스트를 갱신한다.
- [x] **Step 2: Tests** — 실제 PostgreSQL 조회에서 다른 프로젝트·scope·현재/진행 중 try 제외, case_id 연결, 중복 CHAT 제외, cursor 순서 검증. agent HTTP 계약·잘못된 TC·실패·빈 기록·phase 검증. 가능한 로컬 QA 비교를 실행하고 결과와 미실행 사유를 구분한다.
- [x] **Step 3: Rollout / Rollback** — orchestration API를 먼저 적용한 뒤 agent 적용. 옛 서버의 404는 조회 실패로 안내하고 QA를 계속한다. 새 테이블이나 migration은 필요 없다.

## Validation
- 결과: agent 회귀 테스트 290개와 실제 PostgreSQL·HTTP 통합 테스트 9개 통과. 독립 critic review PASS. 실게임 전후 QA 비교는 미실행으로 남긴다.
- Plan review 반영: 종료 시각이 현재 try 시작 시각 이하인 실행만 허용하고 `(started_at, id)` 내림차순 cursor를 사용한다. scope는 `IS NOT DISTINCT FROM`으로 비교한다. 모든 이벤트에 `case` 또는 `run` scope를 표시하고 history 조회 TOOL·TOOL_RESULT는 중첩 방지를 위해 제외한다. tool 설명은 독립 `qa_history/v1` prompt로 두어 QA prompt 구판에서도 읽을 수 있게 한다.
- 최신 develop 반영: step 없는 TC 판정과 중단된 시도도 `qa_case_attempt`로 연결한다. 시도별 snapshot·판정 이유·사람 수정과 질문창 답변을 조회한다. 답변은 별도 cursor로 전부 읽을 수 있다. `v50-tc-history`와 독립 prompt hash를 실행 설정에 남긴다.
- **Commands to run:** agent `.venv/Scripts/python.exe -m pytest` focused suites; orchestration `./mvnw test -Dtest=QaCaseHistoryIntegrationTest` and compilation; qa-run-local prerequisites probe.
- **Expected output:** 과거 실행 조회와 경계 테스트 통과. 과거 결과가 현재 판정의 증거가 아니라는 안내 포함.

## Risks & Rollback
- **Risks:** 옛 로그는 이미 TOOL_RESULT 내용이 잘려 있을 수 있다. CHAT은 TC 단위 attribution이 없어 run scope를 유지한다. TC 명세 원문 snapshot이 없는 실행은 build와 과거 판정만 제공하며 현재 명세로 대체하지 않는다.
- **Rollback steps:** 새 tool 등록과 내부 API 변경을 되돌린다. 기존 로그는 수정하지 않는다.

## Open Questions
- 실게임 비교는 미실행이다. model·prompt_version·시나리오를 고정한 Unity 전후 비교 수치는 없으며 성능 향상을 주장하지 않는다.
