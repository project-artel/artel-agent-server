# 2026-07-30 — QA 런 모델과 reasoning 수준 선택

- Date: 2026-07-30
- Jira: ARTEL-196
- Status: Partial scope approved by chat request; not sufficient to close ARTEL-196

## Goal

`POST /qa-sessions`에서 QA 런별 모델과 모델이 지원하는 reasoning 설정을 선택하고, 세션 저장소와 `QaRunner`를 거쳐 OpenRouter 요청 및 런 시작 로그까지 동일한 값이 전달되게 한다.

## Non-goals

- 비용 승인이 없는 기본 모델 또는 기본 reasoning 예산 변경
- 모델 카탈로그 확장
- 자동 모델 라우팅
- 프롬프트 수정

## Context / Constraints

- 기존 `model` 선택과 `prompt_version` 전달 경로를 재사용한다.
- 공개 요청 형태는 `reasoning: {"effort": "max"|"xhigh"|"high"|"medium"|"low"}` 또는 `reasoning: {"max_tokens": <1 이상의 정수>}`다. 둘은 상호 배타적이며 잘못된 형태/모델 조합은 `POST /qa-sessions`에서 422다.
- 현재 OpenRouter 카탈로그/문서 기준 capability:
  - Claude Sonnet 5, Claude Opus 4.8: `reasoning.effort` (`max`, `xhigh`, `high`, `medium`, `low`)
  - Gemini 2.5 Flash, Gemini 2.5 Pro: `reasoning.max_tokens`
  - GPT-4o mini, GPT-4o, Gemma 4: 미지원
- 미지원 모델에 reasoning을 요청하면 세션 생성 시 명시적으로 거부한다. reasoning 생략은 기존 호출과 완전히 호환된다.
- reasoning 결과 본문은 기존 컨텍스트를 늘리지 않도록 `exclude: true`로 고정한다.
- `exclude: true` 때문에 모델 내부 reasoning block은 타임라인에 오지 않는다. 기존 tool `thought` 기록은 유지된다. 이번 범위는 compute 예산 선택과 설정 재현성이다.
- immutable/hashable reasoning 값이 `build_chat_model` 캐시 키에 포함된다. 같은 모델의 서로 다른 런 설정이 공유 client에서 섞이지 않는다.
- capability는 `ModelSpec` 한 곳에서 관리하고 `GET /models`의 `reasoning` 객체로 노출한다. Orchestration은 이 응답을 캐싱할 수 있으며 별도 capability map을 소유하지 않는다.
- `DEFAULT_MODEL`과 기본 reasoning은 승인 전까지 유지한다.
- 따라서 이번 구현은 사용자가 요청한 선택 기능만 제공한다. 승인값과 실비 검증 없이는 ARTEL-196을 완료 처리하지 않는다.

## Approach (Checklist)
- [x] **Step 0: Recon** 모델 카탈로그, `build_chat_model`, `POST /qa-sessions` → 세션 레코드 → `QaRunner` 흐름과 관련 테스트 확인
- [ ] **Step 1: Implementation** frozen `ReasoningConfig`와 `ModelSpec.reasoning` 단일 capability 추가; `GET /models` catalog API; `OpenQaSessionRequest.reasoning` → `QaSessionRecord.reasoning` → `QaExecutionService` → keyword-only runner factory → `QaRunner` 전달; 공용 validation으로 API와 직접 service 호출 모두 보호; `build_chat_model(model, reasoning)` 한 곳에서 `extra_body.reasoning` 구성; 시작 로그 기록
- [ ] **Step 2: Tests** 기존 요청/이전 Redis JSON은 `reasoning=None`; 선택값 저장·복원 후 runner 도달; 잘못된 조합 422/직접 service 거부; `httpx.MockTransport`로 `/chat/completions`의 정확한 JSON과 생략 시 reasoning 부재; 설정별 cache 격리; 로그에 normalized 설정만 기록; 모든 모델 capability 명시를 집중 테스트 후 전체 pytest 실행. 별도 credential-gated smoke로 Claude effort와 Gemini max_tokens QA 런을 각각 호출하고 usage/비용을 기록
- [ ] **Step 3: Rollout / Rollback** 코드 배포만 필요; 문제 시 변경 커밋 revert

## Validation
- **Commands to run:** `.venv/bin/python -m pytest tests/test_qa_model_reasoning.py tests/test_api.py`; `.venv/bin/python -m pytest`; 승인값과 `OPENROUTER_API_KEY` 확보 후 두 모델 실제 QA smoke
- **Expected output:** 모든 테스트 통과; reasoning 지원 모델만 정확한 `extra_body.reasoning`과 `exclude: true`를 전송; 생략 및 미지원 모델은 reasoning payload 없음

## Risks & Rollback
- **Risks:** OpenRouter 카탈로그가 바뀌면 정적 capability가 오래될 수 있음; LangChain이 알 수 없는 필드를 누락할 수 있음; runner factory 시그니처 변경이 기존 테스트/호출자에 영향
- **Rollback steps:** 변경 커밋 revert. 저장 스키마는 새 필드 기본값이 `None`이라 기존 레코드와 호환

## Open Questions
- 승인 전이므로 새 기본 모델과 기본 reasoning 예산은 이번 변경에서 정하지 않는다.
- 실제 유료 QA 런과 비용 비교는 OpenRouter 키 및 승인된 비교 설정이 없어 수행할 수 없다.
- 위 실호출, 비용 기록, 새 기본값 반영 전에는 ARTEL-196 완료/배포 승인 불가다.
