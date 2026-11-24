# 2026-11-24 — QA Agent Code Mode 추가

- Date: 2026-11-24
- Jira: ARTEL-1466

## Goal

QA Agent가 한 번의 모델 응답으로 관찰과 입력을 조합하고 결과에 따라 다음 도구를 선택할 수 있게 한다.

## Non-goals

외부 프로세스와 파일 접근은 지원 범위 밖이다.

## Context / Constraints

각 run이 제공하는 도구, phase 검사, 실행 예산과 타임라인 기록을 따른다.

## Approach (Checklist)

- 현재 run에 등록된 도구로 호출 목록을 만든다.
- Code Mode에서는 모델에 `run_code` 하나를 제공한다.
- 코드 전체의 문법을 검사한 뒤 조건과 반복에 따라 도구를 순서대로 호출한다.

## Validation

모드별 도구 목록, 조건 분기, 종료, 문법 거절과 실행 기록을 pytest에서 검증한다. 같은 게임 빌드와 시나리오에서 모드를 비교한다.

## Risks & Rollback

여러 도구를 실행한 뒤 오류가 발생하면 앞서 보낸 입력은 게임에 반영된다. 개별 호출 기록을 보존한다. `arch.code_mode`를 끄면 기존 도구 목록으로 돌아간다.

## Open Questions

실제 QA run에서 모델 호출 수와 시나리오 통과율이 어떻게 변하는지 측정해야 한다.
