# 2026-10-29 — Agent PR의 구버전 진단 도구와 evidence·text 기록 오류 수정

- Date: 2026-10-29
- Jira: None
- Status: Complete

## Goal
PR #279의 리뷰 3건을 재현하고 기존 계약을 유지하며 수정한다.

## Non-goals
새 PR·merge·Jenkins 설정 변경·tool schema 변경은 하지 않는다.

## Context / Constraints
공유 worktree를 보존하고 최신 원격 PR head의 독립 worktree를 사용한다. prompt 파일을 새로 복제하지 않는다. tool set·schema·scene/pulse render는 유지한다.

## Approach (Checklist)
- [x] **Step 0: Recon** 3개 리뷰 모두 현재 코드에서 확인했다. FAST REVIEW: PASS — unknown prompt version은 오류를 유지하며 missing role만 fallback한다. MEDIUM REVIEW: PASS — 각 모듈 안에서만 수정한다. HEAVY REVIEW: PASS — indexed 대조·변경 cursor·수용량 경계를 테스트한다.
- [x] **Step 1: Implementation** 진단 description만 명시적 최초 지원 version을 fallback으로 사용한다. indexed selector는 index를 유지해 대조한다. text mark는 변경 revision을 가리켜 기존 줄이 늘어나는 경우도 반환한다.
- [x] **Step 2: Tests** 기존 테스트와 새 회귀 테스트로 구버전·최신 version, 두 객체의 다른 값, mark 이후 연장·drop 경계를 확인하고 전체 suite를 실행한다.
- [x] **Step 3: Rollout / Rollback** 기존 PR 브랜치에 일반 push하고 커밋 revert로 rollback한다.

## Validation
- **Commands to run:** python -m pytest tests/test_qa_observation_prompt_compat.py tests/test_qa_verdict_evidence.py tests/test_qa_macro_shown_text.py; python -m pytest
- **Expected output:** 이전 version에서도 도구 생성, 잘못된 instance 인용 거절, 기존 text의 후속 변경 반환.

## Risks & Rollback
- **Risks:** index 없는 인용의 기존 호환성, bounded text 보존과 mark 경계.
- **Rollback steps:** 이번 commit revert.

## Open Questions
없음. 실제 게임 QA 실행과 개선율 측정은 이번 결함 재현 테스트의 증명 범위가 아니다.


## CI 원인 추가 확인
Jenkins build 2가 Docker test stage의 prompt source 보존 테스트 수집 중 manifest 누락으로 실패했다. 해당 manifest와 보존 source만 test stage에 COPY하며 runtime stage에는 추가하지 않는다. app boot의 Bedrock·삭제 API 테스트는 Redis 의존을 명시적으로 memory로 격리한다. 이 테스트들은 저장소의 Redis 정리 동작을 검증하는 별도 FakeRedis 테스트를 유지한다.
검증 명령에 `docker build --target test`를 추가한다.


## 검증 결과
3개 production 결함은 수정 전 새 테스트 7개 실패로 재현했고 수정 후 대상 27개가 통과했다. prompt loader/lock·arch·진단 도구 계약 164개와 Redis 격리 app 테스트 13개도 통과했다. 최종 전체 suite 2,968개, Docker Python 3.12 test stage의 전체 2,968개가 통과했다. 부모의 독립 diff 검토는 production 수정·Docker·test 격리 모두 PASS였다. 실제 게임 QA나 판정 개선율을 측정한 결과는 아니다.
