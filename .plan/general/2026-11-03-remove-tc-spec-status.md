# 2026-11-03 — TC 명세 등급 status 제거

- Date: 2026-11-03
- GitHub Issue: None
- Jira: ARTEL-1323 (상위 ARTEL-1320)
- Status: Implemented

## Goal
TC 명세 등급을 DB·서버·생성·API·화면에서 제거.

## Non-goals
QA verificationStatus, content map status 및 원본 scenario quality 변경.

## Context / Constraints
기존 미커밋 변경을 보존하고 최신 develop의 별도 worktree에서 이번 변경만 적용했다.

## Approach (Checklist)
- [x] **Step 0: Recon**: 전체 TC 입출력과 export 참조 확인.
- [x] **Step 1: Implementation**: TC JSON·명세 CSV 및 API 응답 모델에서 status 제거. TC 등급별 counts 제거, 명세 projection의 ready_specs/review_specs 집계는 배열 길이와 일치. 원본 scenario quality는 유지.
- [x] **Step 2: Tests**: specs_v2 API·generic·reading·value·observable·gates·gate·answers 테스트 74개 통과.
- [x] **Step 3: Rollout / Rollback**: 서버·DB 계약 변경 후 소비자를 적용한다. 운영 배포는 수행하지 않는다.

## Validation
specs_v2 API·generic·reading·value·observable·gates·gate·answers 테스트 74개 통과.

## Risks & Rollback
TC status/counts를 읽는 외부 소비자는 새 계약에 맞춰야 한다. 서버 ingest는 구 status 입력을 무시한다. rollback은 이 commit revert.

## Open Questions
없음. 알려진 검증 제한은 위에 기록했다.
