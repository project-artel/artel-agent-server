# 2026-11-24 — Planner–Executer 단계별 Game Function 호출 추가

- Date: 2026-11-24
- Jira: ARTEL-1467
- Status: In progress

## Goal

순서형 QA에서 현재 단계에 허용된 준비 함수와 읽기 함수를 게임에 호출하고 SDK 결과를 기록한다.

## Non-goals

FREE 시나리오, 장시간 실행하는 behavior와 SDK 메시지 형식은 이번 작업 범위 밖이다.

## Context / Constraints

Living Dead #454는 빌드가 제공한 무적 설정 함수를 사용할 수 없어 무적 상태를 기다렸다. 검증 단계에서는 준비 함수로 예상 결과를 만들 수 없어야 한다.

## Approach (Checklist)

- [x] 빌드 카탈로그와 실행·TC 권한, 일반 QA 엔진의 호출 경로 확인
- [x] 실제 행동을 실행할 때 이름과 인자를 점검하고 호출 기록 재사용
- [x] 권한, 인자, SDK 응답과 검증 단계 회귀 테스트
- [ ] 배포 후 같은 빌드와 시나리오로 비교

## Validation

Planner–Executer와 Game Function 테스트에서 SDK가 받은 요청, 호출 횟수, 검증 단계 거절과 읽기 결과를 확인한다.

## Risks & Rollback

검증 단계에서 상태를 바꾸는 함수는 TC가 이름으로 허용한 경우에만 호출한다. 준비 함수의 결과 문자열은 실행 기록에 남기지 않고, 실제 게임에서 관찰한 결과로 판정한다. 배포를 되돌리면 기존 엔진의 동작으로 돌아간다.

## Open Questions

실제 게임에서 수정 전후 각 단계의 판정을 비교한다.
