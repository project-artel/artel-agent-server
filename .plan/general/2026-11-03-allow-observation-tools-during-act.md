# 2026-11-03 — QA ACT 관찰 도구 허용 수정

- Date: 2026-11-03
- Jira: ARTEL-1333
- Status: 로컬 수정 및 회귀 테스트 완료, 실행 서버 반영 전

## Goal

ACT에서도 `capture_screen`과 `inspect_object`를 호출해 입력 결과를 확인한다.

## Non-goals

`fold` 정책, 판정 기준, 실행 중인 QA의 결과는 변경하지 않는다.

## Context / Constraints

`observe_scene`은 항상 허용되지만 나머지 두 관찰 도구는 OBSERVE에 묶여 있다.
UPDATE_MEMORY와 REVIEW_DRAFT의 필수 응답 조건은 유지한다.

## Approach (Checklist)
- [x] **Step 0: Recon** — phase gate와 기존 테스트 확인.
- [x] **Step 1: Implementation** — ACT에서 두 관찰 도구를 허용하고 phase를 유지. 최신 develop 기준 구조 label을 `v48-act-observation`으로 갱신.
- [x] **Step 2: Tests** — lite/full에서 입력, 관찰, 추가 입력, 판정 순서와 실제 도구 본문 실행 검증. 다른 phase의 제한 회귀 검증.
- [ ] **Step 3: Rollout / Rollback** — 적용에는 agent-server 재시작 필요. 실행 중인 QA 중단은 피한다.

## Validation
- **Commands to run:** `python -m pytest tests/test_qa_phase_cycle.py tests/test_qa_arch.py tests/test_qa_tools.py -q`
- **Result:** 최신 develop (`ef96b66`) 기준 191 passed. 판정 도구 이름 `report_test_case`에 맞춰 회귀 테스트를 이식했다. `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false`로 실행. 별도 critic 검토의 도구 이름과 label 수정 의견을 반영했다.
- 실제 게임 QA 전후 비교는 미수행. 현재 서버는 `/home/yunseong/dev/artel/.work/latest-pr-local-20261103/artel-agent-server`에서 실행 중이며 이번 Windows checkout 수정이 반영되지 않았다. 일시 정지된 QA를 유지하기 위해 프로세스를 재시작하지 않았다.

## Risks & Rollback
- **Risks:** 관찰 도구가 판정/기록 응답을 대신하면 단계 순서가 깨질 수 있다. ACT만 예외로 처리한다.
- **Rollback steps:** 이번 수정 파일의 해당 diff만 되돌린 뒤 agent-server 재시작.

## Open Questions
- 없음.
