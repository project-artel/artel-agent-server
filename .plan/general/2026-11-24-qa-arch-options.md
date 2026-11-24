# 2026-11-24 — QA 실행 고급 설정 목록 API 추가

- Date: 2026-11-24
- Jira: ARTEL-1472

## Goal

Home에서 조정할 수 있는 QA 실행 설정을 Agent 서버에서 조회한다.

## Non-goals

QA 실행 모델, 설정 기본값, 검증 조건을 바꾸지 않는다.

## Context / Constraints

기존 선택으로 만든 run label과 저장된 선택을 보존하려면 기존 식별자와 선택값 순서를 유지해야 한다. 실행 예산과 제한 시간은 공개 범위 밖에 둔다.

## Approach (Checklist)

- [x] 기존 Home 설정 13개에 대응하는 `QaArchSpec` 필드와 Code Mode에 화면용 메타데이터를 표시한다.
- [x] 표시한 필드의 타입과 제약으로 내부 조회 응답을 구성한다.
- [x] 응답의 공개 범위, 선택값 순서, 조건과 타입을 검사한다.

## Validation

`pytest tests/test_qa_arch_options.py tests/test_qa_arch.py tests/test_api_internal_prefix.py`에서 HTTP 응답, 기존 구조 fingerprint, API 경로 경계를 검사한다.

## Risks & Rollback

옵션 식별자와 선택값 순서를 바꾸면 이전 run과 같은 선택이 다른 label로 기록된다. 계약 테스트에서 두 값을 고정한다. 서버 변경을 되돌리면 조회 경로가 사라지므로 Home의 기본 실행 경로가 먼저 배포돼 있어야 한다.

## Open Questions

없음.
