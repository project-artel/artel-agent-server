# 2026-11-03 — OpenAI API 캐시 사용량 기반 추정 비용 추가

- Date: 2026-11-03
- Jira: ARTEL-1331 (상위 ARTEL-1329, 단가 등록 ARTEL-1330)
- Status: Implemented / draft PR 준비 (운영 미적용)

## Goal

`gpt/` 직접 호출의 토큰을 공식 Standard 단가로 계산하고 `costEstimated=true`로 기록한다. 일반 입력, 캐시 읽기, 기본 30분 캐시 쓰기, 출력은 각각 한 번만 계산한다.

## Non-goals

운영 배포, 과거 사용량 재계산, UI 변경, 12시간 캐시 및 다른 service tier 추가는 제외한다.

## Context / Constraints

모델 단가는 orchestration `llm_model.pricing`이 원본이다. 기존 `_estimate_cost`는 `inputTokens` 밖의 cache write를 별도로 계산한다. OpenAI Responses의 `input_tokens`에는 cache write가 포함되므로 callback의 기존 `input_includes_cache_write`를 사용한다. 설치된 langchain-openai는 `cache_write_tokens`를 `cache_creation`으로, `cached_tokens`를 `cache_read`로 전달한다. reasoning은 이미 출력 합계에 포함된다.

요청의 `service_tier`는 `default`로 명시해 Standard 단가와 맞춘다.

공식 문서(2026-11-03): https://developers.openai.com/api/docs/pricing 및 각 모델 페이지. 기본 30m write는 입력 단가의 1.25배다. 272,000 초과 입력에는 요청 전체 입력·캐시 2배, 출력 1.5배를 적용한다. 모델별 입력/읽기/쓰기/출력 단가는 Sol 2/0.1/2.5/10, Astra 10/1/12.5/50, Luna 0.1/0.01/0.125/0.5, Terra 2/0.2/2.5/12 달러/백만 토큰이다.

## Approach (Checklist)
- [x] **Step 0: Recon** — 모델 등록, callback, SDK 변환과 공식 단가 확인.
- [x] **Step 1: Implementation** — 새 Flyway migration으로 네 `gpt/` 모델의 단가 등록. `_gpt`에 cache write 포함 플래그 적용. 기존 테스트 catalog도 같은 값으로 갱신.
- [x] **Step 2: Tests** — 실제 Responses 응답을 SDK로 변환한 뒤 callback 기록 검증. 캐시 없음/읽기/쓰기/혼합/전량 캐시, 272K 경계와 초과, reasoning 중복 방지, 기존 provider 금액 우선 및 단가 미상 회귀 검증.
- [ ] **Step 3: Rollout / Rollback** — agent 토큰 정규화를 먼저 반영하고 orchestration migration을 적용한다. 반대 순서면 기존 agent가 캐시 쓰기를 일반 입력에도 포함해 중복 계산하므로 금지한다. 변경된 네 단가만 NULL로 돌리면 추정을 중단할 수 있다. 운영에는 적용하지 않는다.

## Validation
- **Commands to run:** `python -m pytest tests/test_llm_usage.py tests/test_llm_user_key.py tests/test_llm_catalog.py` 및 관련 LLM 회귀 테스트. PostgreSQL에서 migration 적용과 네 row 검증.
- **Result:** `python -m pytest tests/test_llm_*.py -q` — 286 passed. 로컬 PostgreSQL 임시 테이블에서 migration 실행, 기존 단가 보존 및 재실행 검증 후 rollback. SQL 결과와 agent fixture의 네 단가가 일치한다. 최종 pair review PASS. 실제 유료 OpenAI 요청과 운영 배포는 실행하지 않았다.

## Risks & Rollback
- **Risks:** 단가는 변경될 수 있으므로 출처와 확인일을 저장한다. 기본 Standard/30m 경로만 계산한다. 기존 dirty checkout의 사용자 변경은 보존한다.
- **Rollback steps:** 이번 변경만 revert하고 해당 `gpt/` row 단가를 NULL로 복구한다. 과거 기록은 수정하지 않는다.

## Review

계획 검토에서 rollout 순서를 agent 우선으로 수정했다. 코드 검토에서 `service_tier=default` 지정과 payload 검증을 추가했다. 두 검토 모두 최종 PASS다.

## Open Questions
- 없음.
