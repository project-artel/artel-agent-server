# 2026-07-29 — LangSmith trace 식별 정보 보강

- Date: 2026-07-29
- Jira: ARTEL-144
- Status: Complete

## Goal

LangSmith에 이미 전송되는 최상위 에이전트 실행에 안정적인 이름과 세션·모델 메타데이터를 추가해 실행별 조회와 필터링이 가능하게 한다.

## Non-goals

- LangSmith 인증 및 전송 방식 변경
- 에이전트 프롬프트, 결과 또는 실행 흐름 변경
- FastAPI 전체 요청이나 외부 서비스 사이의 분산 tracing 추가

## Context / Constraints

- 기존 `configure_langsmith` 환경변수 기반 자동 tracing을 유지한다.
- trace 설정은 각 최상위 `ainvoke`/`astream` 경계에만 적용한다.
- 민감하거나 큰 입력값은 metadata에 넣지 않는다.

## Approach (Checklist)
- [x] **Step 0: Recon** (기존 LangSmith 설정과 LangChain 호출 경계 확인)
- [x] **Step 1: Implementation** (`AgentContext` trace config helper 및 QA runner correlation metadata 적용)
- [x] **Step 2: Tests** (전달되는 run config 단위 테스트 추가, 구문 및 diff 검사)
- [x] **Step 3: Rollout / Rollback** (`LANGSMITH_TRACING` 플래그 유지, 변경 revert 가능)

## Validation
- **Commands to run:** `PYTHONPYCACHEPREFIX=/private/tmp/artel-agent-pyc python3 -m compileall -q app tests`, `git diff --check`; 의존성이 설치된 환경에서 `python -m pytest`
- **Expected output:** 구문 및 diff 검사 통과, runnable에 예상 trace config 전달. 현재 작업 환경에는 pytest가 없어 전체 테스트는 미실행.

## Risks & Rollback
- **Risks:** Runnable config 키 오타로 trace 분류가 누락될 수 있음
- **Rollback steps:** trace config 전달 변경만 revert; 기존 환경변수 tracing은 유지

## Open Questions
- 없음
