# 2026-11-24 — Jenkins agent server blue/green 배포 추가

- Date: 2026-11-24
- Jira: ARTEL-1470

## Goal

새 요청은 준비된 버전이 받고 기존 세션은 원래 컨테이너에서 끝낸다.

## Non-goals

실행 중인 모델 호출이나 WebSocket을 다른 프로세스로 옮기지 않는다.

## Context / Constraints

기존 Jenkins 브랜치 두 개가 같은 Docker 환경을 공유한다. QA와 작성 세션은 POST와 후속 WebSocket 사이에도 살아 있다.

## Approach (Checklist)

1. 두 세션 ID에 배포 슬롯을 표시한다. 기존 ID는 원래 서버에서 처리한다.
2. 내부 nginx에서 새 요청의 목적지만 전환한다. 환경별 Jenkins 잠금을 사용한다.
3. Redis 또는 메모리의 살아 있는 세션, 처리 중인 요청과 기존 nginx worker를 확인하고 이전 컨테이너를 종료한다.

## Validation

두 슬롯과 라우터를 실제 Docker 네트워크에 띄워 연결 전후 전환, 롤백, 중단 후 재시도를 확인한다.

## Risks & Rollback

첫 전환에는 orche의 HTTP와 WebSocket 주소 변경이 필요하다. 구버전 컨테이너는 보존한다. 이후 롤백도 신규 요청의 목적지만 되돌린다.

## Open Questions

없음.
