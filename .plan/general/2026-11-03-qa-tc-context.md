# 2026-11-03 — QA TC snapshot 조회와 압축 후 진행 상태 보존 추가

- Date: 2026-11-03
- Jira: ARTEL-1332
- Status: Implemented; live A/B 미실행

## Goal

QA #9처럼 compaction 후 명세를 사용자에게 재요청하지 않도록 한다. 전체 TC ID, 판정 기록이 있는 ID, 아직 판정이 없는 ID, 현재 검증 중 ID는 실제 run 데이터에서 생성해 유지한다. 원문은 실행 당시 snapshot에서 조회한다.

## Non-goals

DB/public API 변경, 실행 중 서비스 재시작, 기존 run 강제 재개, 게임 데이터 변경, 모델 품질 개선 주장.

## Context / Constraints

최신 develop을 기반으로 별도 worktree에서 작업한다. snapshots는 이미 channel.evaluation에 있다. 전체 ID는 중복 없이 시나리오 순서로 유지하며 snapshot이 없는 case_id도 숨기지 않는다. PASSED/FAILED/INCONCLUSIVE 모두 판정 기록으로 분류하고 active 재검증은 별도로 나타낸다. 저장 실패 시 active 상태는 유지한다.

## Approach (Checklist)
- [x] **Step 0: Recon** — runner._plan, compaction.render_progress_ledger, reporting_tools, QaEvaluationClient와 기존 테스트 확인.
- [x] **Step 1: Implementation** — get_test_case(case_id)는 이 run의 snapshot만 읽고 없는 ID와 누락 snapshot은 명시한다. start_test_case는 시작 성공 후 원문과 active 상태를 반환한다. 명세 JSON이 약 4,000 token을 넘으면 초기 계획에는 전체 ID·제목·scene 목록만 싣고 원문과 중복 reference_steps는 생략한다.
- [x] **Step 1: Implementation** — before_model middleware가 전체/판정 완료/미검증/active ID와 현재 snapshot을 생성한다. 고정 ID를 가진 ledger message를 유지하고, 상태가 바뀌거나 compaction으로 메시지가 없어졌을 때만 갱신한다. 같은 상태에서는 message를 다시 쓰지 않아 prompt cache를 매 호출 깨지 않는다.
- [x] **Step 1: Implementation** — compaction의 요약 입력에서 authoritative ledger와 명세 block은 제외한다. 압축 직후 동일 원본 데이터에서 ledger를 복원한다. get/start 원문 응답은 marker로 구분해 현재 active TC와 가장 최근 조회를 제외한 오래된 상세를 fold한다. 원본 snapshot과 timeline/log는 수정하지 않는다.
- [x] **Step 1: Implementation** — QA_ARCH_LABEL과 middleware/tool fingerprint를 갱신한다.
- [x] **Step 2: Tests** — 원문을 누락하는 fake summarizer로 반복 compaction, 조회, 큰/작은 예산, 중복·없는 ID, active/재검증/저장 실패, 오래된 상세 fold, cache를 위한 ledger 무변경을 검증한다. 관련 QA/architecture/phase 테스트를 실행한다.
- [x] **Step 3: Rollout / Rollback** — 구현을 검토하고 draft PR을 만든다. 서비스 재시작 금지 요청에 따라 실제 게임 A/B 실행은 생략하고 draft에 한계를 기록한다.

## Validation
- **Commands to run:** 기존 venv의 python -m pytest로 TC context·compaction·runner·architecture·reporting·phase 관련 테스트 실행.
- **Expected output:** 전체 ID와 active 원문이 반복 압축 후에도 모델 입력에 나타나고 get_test_case가 정확한 snapshot을 반환한다. 기존 phase/architecture 계약을 보존한다.

## Risks & Rollback
- **Risks:** 매우 많은 TC는 전체 ID 목록 자체가 커질 수 있다. 전체 ID를 생략하지 않는 요구를 우선한다. before_model node 추가에 따른 recursion_limit을 반영한다. 모델의 실제 판단 품질은 deterministic 테스트만으로 입증하지 않는다.
- **Rollback steps:** 변경 commit을 revert한다. DB migration은 없다.

## Open Questions
- 없음. 현재 검증 중인 ID는 start 성공 후 지정하고 그 ID의 report 저장 성공 후 해제한다. 다른 TC를 시작하면 마지막으로 시작한 TC를 현재 대상으로 표시하고 아직 종료하지 않은 시도 ID들도 별도로 유지한다.

## Plan Review

- FAST REVIEW: NONPASS — 기존 snapshot은 reviews를 제외한다. 초기 계획에 적용한 human_review를 별도로 실행 상태에 보관하여 get/start/active 원문에서 동일하게 제공한다. 원문 TC와 사람의 이전 판정은 구별하고 이전 판정을 정답으로 취급하지 않는 기존 지시도 함께 보존한다.
- MEDIUM REVIEW: PASS — compaction과 새 middleware는 같은 TC 상태 renderer를 사용한다. get_test_case는 모든 phase에서 허용한다. 현재 active 외에 최근 verdict와 INCONCLUSIVE ID를 명시한다.
- 검증 추가: create_agent + fake model로 start/report/재검증과 반복 compaction을 구동하여 모델이 항상 단 하나의 authoritative TC ledger를 읽는지 확인한다.


## Implementation Review

- HEAVY REVIEW: PASS
- pair-review-critic VERDICT: PASS
- TC·compaction·phase·context·architecture 관련 테스트와 QA tools·runner·run_config 회귀 테스트 총 267개 통과.
- 실제 게임 A/B 비교는 실행 중 서비스 재시작 금지 조건 때문에 수행하지 않았다. 현재 QA #9에는 변경을 적용하지 않았다.
