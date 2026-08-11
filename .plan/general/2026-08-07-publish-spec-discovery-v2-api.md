# 2026-08-07 — v2 명세 발견기와 internal API 배포

- Date: 2026-08-07
- Jira: None
- Status: Complete

## Goal

이번 대화에서 검증한 deterministic composite-evidence v2 구현만 제품 패키지로 편입하고, orchestration-server가 SDK JSON 한 건을 POST해 Ready·Candidate·Review·Connected Flow를 구조화된 JSON으로 받는 stateless internal endpoint를 제공한다. 변경 범위를 독립 브랜치와 단일 PR로 게시한다.

## Non-goals

- 기존 `app/affordance/preprocess`, Spec/Polish agent, `prototype/` 구현을 포함하거나 변경하지 않는다.
- Editor와 Devbuild 결과를 한 요청에서 통합·비교하지 않는다.
- LLM 호출, DB 저장, 파일 기반 산출물 생성을 endpoint 처리 경로에 넣지 않는다.
- 생성된 CSV/XLSX/분석 샘플을 제품 커밋에 포함하지 않는다.

## Context / Constraints

- 현재 작업트리는 기존 ARTEL-177 브랜치와 여러 무관한 tracked/untracked 변경을 포함하므로 `origin/develop` 기반 전용 브랜치에서 명시적 경로만 stage해야 한다.
- 요청 본문은 wrapper가 아닌 SDK JSON 자체이며 schema 5/6만 허용한다.
- v2는 같은 입력에 deterministic해야 하고 endpoint는 메모리 안에서만 결과를 조립해야 한다.
- GitHub CLI 인증 토큰은 사전 점검에서 만료 상태이므로 push/PR 단계에서 실제 사용 가능한 인증 경로를 확인한다.

## Approach (Checklist)
- [x] **Step 0: Recon** (브랜치·API·패키징·v2 파일 범위와 무관한 작업트리 변경 확인)
- [x] **Step 1: Implementation** (`app/specs_v2`, pure payload projection, `POST /internal/specs/v2/generate`, OpenAPI wiring)
- [x] **Step 2: Tests** (schema 오류, deterministic 응답, Editor/Devbuild 독립성, candidate 경계, 전체 pytest)
- [x] **Step 3: Rollout / Rollback** (명시적 stage, 범위 검토, commit, push, draft PR)

## Validation
- **Commands to run:** v2 단위 테스트; internal API focused pytest; `python -m pytest`; 동일 요청 2회 응답 비교; `git diff --cached --stat` 및 staged 파일 목록 검토
- **Expected output:** schema 5/6 SDK JSON은 200과 독립 v2 payload를 반환하고, 잘못된 schema는 422다. 같은 입력의 응답은 동일하며 PR에는 v2·endpoint·테스트·이번 계획만 포함된다.
- **Observed:** v2 회귀 테스트 22개와 API/라우팅 테스트 27개가 통과했다. 최신 Editor/DevBuild 산출물은 각각 200으로 독립 처리됐고 동일 입력 재실행 결과가 일치했다. 전체 pytest는 493개 중 492개가 통과했으며, `origin/develop`에 이미 존재하는 `qa_run/v9` prompt-lock 누락 1건만 실패했다(이번 diff 밖).

## Risks & Rollback
- **Risks:** 큰 SDK JSON이 동기 CPU 작업으로 event loop를 점유할 수 있다. v2 내부 모델 전체를 응답하면 payload가 과대해질 수 있다.
- **Rollback steps:** internal router 등록을 제거하면 외부 동작은 즉시 원복된다. 패키지와 endpoint가 다른 기존 API 상태에 의존하지 않아 커밋 단위 revert가 가능하다.

## Open Questions
- 없음. endpoint는 `/internal/specs/v2/generate`, 요청은 raw SDK JSON, 응답은 요약과 `ready_specs`, `review_specs`, `connected_flows`로 구현한다.
