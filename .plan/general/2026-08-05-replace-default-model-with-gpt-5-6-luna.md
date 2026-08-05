# 2026-08-05 — 기본 모델을 GPT-5.6 Luna로 교체

- Date: 2026-08-05
- Jira: ARTEL-241
- Status: Draft

## Goal

`LLMModel` 카탈로그에서 `gpt_4o_mini`를 빼고 `openai/gpt-5.6-luna`를 넣는다. `DEFAULT_MODEL`이 Luna를 가리키게 해서, 모델을 지정하지 않은 모든 경로(시나리오 생성, `/extract`, QA 런)가 추론 가능한 모델로 돈다.

## Non-goals

- `chat_model.py`의 `temperature=0.2` 정리. Luna는 `temperature`를 지원 파라미터로 광고하지 않아 OpenRouter가 버린다. 별건이다.
- `gpt_4o` 제거. 4o 계열 정리 작업이 아니다.
- 기본 reasoning effort를 코드에서 고정하는 것. OpenRouter 기본값 `medium`을 그대로 쓴다.
- Orchestration 쪽 기본값 수정. ARTEL-239 소관이다.

## Context / Constraints

OpenRouter 카탈로그 실측(`GET /api/v1/models`):

| | gpt-4o-mini | gpt-5.6-luna |
|---|---|---|
| in/out (per M) | $0.15 / $0.60 | $0.10 / $0.60 |
| 캐시 읽기 | $0.075 | $0.01 |
| 컨텍스트 | 128K | 1.05M |
| `structured_outputs` | 있음 | 있음 |
| `input_modalities` | text, image, file | text, image, file |
| `reasoning` | 없음 | effort (max/xhigh/high/medium/low/none), 기본 medium |

`ReasoningEffort` enum은 `none`을 제외한 5개를 담고 있어 `tuple(ReasoningEffort)`가 그대로 맞는다.

제약:

- 능력 플래그는 카탈로그 실측으로만 채운다. `models.py` 주석이 요구하는 규칙이고, Gemma 4가 티켓 설명만 믿고 text-only로 등록됐다가 캡처 툴을 잃은 전례가 있다.
- `tests/test_qa_model_reasoning.py`가 `gpt_4o_mini`를 "추론 미지원 OpenAI 모델" 픽스처로 3곳에서 쓴다. 슬러그가 사라지면 그 의미를 `gpt_4o`가 이어받아야 한다. Luna로 바꾸면 테스트 의도가 뒤집힌다.
- Orchestration `TestScenarioAgentService.kt:59`가 `artel.agent.model` 기본값으로 `openai/gpt-4o-mini`를 하드코딩한다. 이 PR이 먼저 배포되면 프로퍼티를 오버라이드하지 않은 환경은 422를 받는다. ARTEL-239와 함께 나가야 한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `gpt_4o_mini` 참조는 `app/llm/models.py` 3곳, `tests/test_agents_scenario.py` 1곳, `tests/test_qa_model_reasoning.py` 3곳. 그 외 코드는 전부 `DEFAULT_MODEL` 경유라 손댈 필요 없다.
- [ ] **Step 1: Implementation**
  - `app/llm/models.py`: enum 멤버를 `gpt_5_6_luna = "openai/gpt-5.6-luna"`로 교체, `MODEL_SPECS` 항목을 Luna 스펙(`reasoning=ReasoningKind.effort`, `reasoning_efforts=tuple(ReasoningEffort)`, `input_modalities=("text", "image", "file")`)으로 교체, `DEFAULT_MODEL` 갱신.
- [ ] **Step 2: Tests**
  - `tests/test_agents_scenario.py:244` — strict json 대상 모델을 Luna로.
  - `tests/test_qa_model_reasoning.py` — 추론 미지원 픽스처 3곳을 `gpt_4o`로 옮기고, Luna의 catalog reasoning 항목을 검증하는 단언을 추가한다.
  - `python -m pytest`.
- [ ] **Step 3: Rollout / Rollback** — 코드 변경뿐이라 마이그레이션 없음. 배포 순서만 ARTEL-239와 맞춘다.

## Validation

- **Commands to run:** `python -m pytest`
- **Expected output:** 전체 통과. Luna가 `json_schema` 경로를 타고, catalog에 effort 목록이 실리고, `gpt_4o`가 추론 미지원으로 계속 거부되는 것을 단언이 확인한다.
- **실측(참고, 이 PR에서 재실행 불필요):** scenario agent 1턴 3회 — 4o-mini 4.9~6.1초/$0.00031/스텝 5, Luna 7.3~9.5초/$0.00073/스텝 7. Luna만 요청한 엣지 케이스 2개를 올바르게 커버했다.

## Risks & Rollback

- **Risks:**
  - Orchestration이 먼저 배포된 채로 남으면 세션 개설 422. 완화: ARTEL-239와 함께 배포, 임시로는 `ARTEL_AGENT_MODEL` 오버라이드.
  - 회당 비용 2.3배, 지연 1.5배. 절대액은 $0.001 미만이라 수용한다.
  - 저장된 요청·설정에 `openai/gpt-4o-mini` 문자열이 남아 있으면 이제 검증에서 거부된다. 현재 영속 데이터가 없어 실제 영향은 Orchestration 프로퍼티뿐이다.
- **Rollback steps:** `git revert`. 상태를 남기지 않는 변경이라 되돌리면 끝이다.

## Open Questions

- 없음.
