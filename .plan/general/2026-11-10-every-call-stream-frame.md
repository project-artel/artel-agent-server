# 2026-11-10 — every_call 스트리밍 프레임 적용

- Date: 2026-11-10
- Jira: None
- Status: Draft

## Goal

`every_call`에 orchestration이 받은 최신 JPEG를 한 장 제공한다.

## Non-goals

`on_demand`와 `by_role`은 기존 캡처 경로를 사용한다.

## Context / Constraints

브라우저 thumbnail은 사용자 인증을 사용한다. Agent는 내부 포트에서 QA 시도에 연결된 녹화의 프레임을 읽는다.

## Approach (Checklist)

- [x] `JPEG_WS` 중계와 thumbnail 경로를 확인한다.
- [x] 1초 이내의 JPEG를 내부 API로 제공하고, Agent에서 조회 실패 시 SDK 캡처로 이어간다.
- [ ] 성공과 fallback 경로를 검증한다.
- [ ] orchestration API PR과 Agent PR의 배포 순서를 기록한다.

## Validation

- **Commands to run:** `python -m pytest tests/test_qa_capture.py tests/test_qa_arch.py`
- **Expected output:** 최신 JPEG는 한 장만 전달되고 응답이 없으면 기존 캡처가 실행된다.

## Risks & Rollback

- **Risks:** 스트림 수신 시각은 게임 안의 사건과 프레임을 직접 연결하지 않는다.
- **Rollback steps:** Agent 변경을 revert하면 SDK 캡처로 복구된다.

## Open Questions

- 실제 QA에서 기존 캡처와 스트리밍 경로의 대기 시간을 비교한다.
