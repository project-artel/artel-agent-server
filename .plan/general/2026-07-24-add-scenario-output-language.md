# 2026-07-24 — 테스트 시나리오 출력 언어 지정 기능

- Date: 2026-07-24
- Jira: None
- Status: Implemented (테스트 통과)

## Goal

시나리오 에이전트(`/sessions` 계열)가 생성하는 자연어 출력의 언어를 호출자가 지정할 수 있게 한다.
지정이 없으면 한국어(`ko`)를 기본값으로 사용한다.

대상은 `ScenarioAgentResult`의 자연어 값 **전부**다. 즉 프론트 UI가 그대로 렌더/등록하는 값들:

- `message` — 챗봇 응답 (UI 대화창에 그대로 노출)
- `scenario.title`, `scenario.description`
- `scenario.steps[*].title`, `.state`, `.action`, `.expected`

`step`(번호)과 JSON 키는 불변이다. 위 목록이 곧 결과 스키마의 문자열 필드 전체이므로,
"일부만 번역되는" 중간 상태는 허용하지 않는다.

`state` / `action` / `expected`는 코드 식별자를 섞지 않은 **순수 자연어 문장**을 유지한다.
`BuyButton을 누른다`가 아니라 `구매 버튼을 누른다`.

## Non-goals

- `/extract`(game_context 추출) 에이전트에는 적용하지 않는다. game_context는 내부 데이터로만 쓰이고
  영문 고정이 프롬프트 재사용·토큰 효율 측면에서 유리하다.
- 서버 로그/에러 메시지/API 필드명 등 시스템 텍스트의 다국어화는 범위 밖이다. JSON 키는 항상 영어를 유지한다.
- 실제 번역 품질 검증(사람 리뷰, 언어별 평가셋)은 이번 범위 밖이다.
- `user.language` 칼럼 추가/마이그레이션/조회는 **오케스트레이션 저장소**의 작업이다. 이 저장소에는
  사용자 개념도 RDB도 없으므로, 여기서는 요청 본문으로 받은 값을 쓰는 것까지만 한다.

## Context / Constraints

현재 시나리오 경로의 파라미터 전달 흐름은 `model` 하나가 이미 정확히 같은 모양으로 존재한다.

```
OpenSessionRequest.model  ──▶ SessionService.open ──▶ SessionRecord.model (Redis 저장)
TurnMessage.model (Optional, 턴 단위 override) ──▶ SessionService.run_turn ──▶ SessionRecord.model
                          ──▶ ScenarioAgentRequest.model ──▶ ScenarioAgent.run
```

관련 파일:
- [app/agents/scenario/prompt.py](app/agents/scenario/prompt.py) — `SYSTEM_PROMPT`, `HUMAN_TEMPLATE`, `build_chain_inputs`
- [app/agents/scenario/schemas.py](app/agents/scenario/schemas.py) — `ScenarioAgentRequest`
- [app/sessions/schemas.py](app/sessions/schemas.py) — `SessionRecord`
- [app/sessions/service.py](app/sessions/service.py) — `open` / `run_turn` / `_generate`
- [app/api/sessions.py](app/api/sessions.py) — `OpenSessionRequest`, `TurnMessage`
- [app/llm/models.py](app/llm/models.py) — enum + 기본값 + 카탈로그 조회 패턴의 참고 사례

### 값의 출처: 오케스트레이션의 `user.language` 칼럼

언어 설정의 **source of truth는 이 저장소가 아니다.** 오케스트레이션 서버의 `user` 테이블에
`language` 칼럼(default `ko`)을 추가하고, 세션을 열 때 그 값을 읽어 이 서버로 넘긴다.

```
[orchestration] user.language (DB, default 'ko')
      └─▶ POST /sessions { ..., "language": "ko" }   ← 첫 턴 포함 전 구간에 적용
      └─▶ WS turn { ..., "language": "en" }          ← 세션 도중 전환 시에만
```

이 저장소에는 사용자 개념도, RDB도 없다(세션은 Redis에 TTL로만 산다). 따라서 **이 플랜의 범위에
마이그레이션이나 유저 조회는 포함되지 않는다** — 에이전트 서버는 요청 본문으로 받은 값을 그대로 쓴다.
`user` 테이블 칼럼 추가와 조회는 오케스트레이션 저장소의 별도 작업이며, 두 작업의 계약은
"요청 본문의 `language` 키, 값은 `ko` | `en`" 하나다.

에이전트 서버 쪽 기본값 `ko`는 DB 기본값과 같은 값이지만 **역할이 다르다.** DB 기본값은 사용자 설정의
초기값이고, 이쪽 기본값은 호출자가 키를 누락했을 때의 방어선이다. 두 곳이 우연히 같은 값일 뿐이므로
한쪽을 바꾼다고 다른 쪽이 따라가지 않는다는 점을 문서에 남긴다.

### 다운스트림 소비자: QA Execution Agent

시나리오는 사람이 읽는 문서이자, 추후 QA Execution Agent의 입력이다. 실행 에이전트는 실행 시점에
**step 단위로 현재 씬에서 불러올 수 있는 정보(state / event / function 등)를 받아 단서로 삼고,
그중 조건에 맞는 것을 골라 invoke**하는 방식으로 동작할 계획이다.

즉 바인딩은 이런 모양이다:

```
step.action = "구매 버튼을 누른다"          ← 시나리오가 주는 의도
scene candidates (실행 시점 주입)          ← 실행 에이전트가 받는 단서
  function: ShopUI.OnBuyButtonClicked()
  function: ShopUI.OnCloseButtonClicked()
  state:    PlayerWallet.gold
  event:    PurchaseCompleted
→ 에이전트가 후보 중 매칭 선택
```

의도 → 함수 바인딩은 **실행 시점 에이전트의 책임**이고 시나리오 생성 시점의 책임이 아니다.
시나리오는 의도(intent)를 자연어로 기술하는 계층에 머문다. 이 분리가 결정 5의 근거다.

이 구조가 두 가지를 확정한다:

1. **시나리오에 식별자를 박을 이유가 없다.** 단서는 런타임 씬에서 온다. 오히려 생성 시점에 박아 둔
   식별자는 씬이 바뀌면 런타임 후보 목록과 충돌하는 낡은 정보가 되어 매칭을 방해한다.
2. **자연어의 구체성 요구가 낮아진다.** 자유 바인딩이 아니라 **후보 목록 안에서의 선택**이므로,
   문장은 절대적으로 정밀할 필요가 없고 후보들 사이에서 **구별 가능**하기만 하면 된다.
   `구매 버튼을 누른다`는 위 후보 목록에서 충분히 판별된다. `상점에서 뭔가 한다`는 여전히 부족하다.

제약:
- `SessionRecord`는 Redis에 JSON으로 저장된다. 새 필드는 반드시 default를 가져야 기존 세션 레코드가 깨지지 않는다.
- 출력은 `with_structured_output`으로 강제되는 구조화 JSON이다. 언어 지시가 스키마를 흔들면 안 되므로
  스키마는 건드리지 않고 프롬프트 지시만 추가한다.

## Approach (Checklist)

### 설계 결정 (왜 이 방식인가)

**결정 1 — 키 이름은 `language`, 값은 `OutputLanguage` StrEnum(`ko` / `en` 2종).**
API 요청 본문의 키는 `language`. 값은 자유 문자열이 아니라 enum이다 — 자유 문자열을 그대로 프롬프트에
넣으면 호출자가 넣은 문장이 시스템 지시로 섞이는 프롬프트 인젝션 경로가 되고, enum이면 FastAPI가
경계에서 422로 거른다. `ko`(한국어) / `en`(영어) 둘만 지원하고 기본값은 `DEFAULT_LANGUAGE = ko`.
`ja` / `zh`는 검증 없이 API 계약에 올리지 않는다 — 필요해지면 enum 한 줄과 지시문 상수 하나로 추가된다.

**결정 2 — 정의 위치: `app/agents/scenario/schemas.py`.**
적용 범위가 시나리오 에이전트뿐이라는 사실을 타입 위치로 표현한다. `app/llm/`에 두면 "모든 LLM 호출에
적용되는 설정"으로 읽혀 non-goal과 어긋난다. 다른 에이전트가 필요해지는 시점에 `app/llm/languages.py`로 승격한다.

**결정 3 — 전달 경로: `model`과 동일한 3단(세션 오픈 → 레코드 → 턴 override).**
새 축을 만들지 않고 이미 검증된 경로를 그대로 따라간다. 세션 도중 언어 전환 요구가 실제로 있고,
`model`이 이미 그 형태를 지원한다.

여기서 짚을 점: 적용 대상은 `wss://` 시나리오 경로지만, **키는 `POST /sessions` 본문에도 반드시 넣어야 한다.**
첫 턴은 WS `turn` 메시지가 아니라 세션 오픈 시 저장된 `pending_user_input`으로 `start_first_turn`이 돌린다
([app/sessions/service.py:46](app/sessions/service.py:46)). `POST /sessions`에 `language`가 없으면
**첫 결과만 기본 언어로 오고 두 번째 턴부터 지정 언어가 되는** 눈에 띄는 버그가 된다.
WS `turn`의 `language`는 세션 도중 전환용 Optional override로 남긴다.

**결정 4 — 프롬프트 주입 위치: 시스템 메시지에 전용 변수 한 개.**
`build_scenario_prompt()`의 system 메시지를 `{language_directive}` 변수를 포함한 템플릿으로 바꾸고,
`build_chain_inputs`에서 언어별 지시문을 렌더한다. 언어 규칙은 역할·제약 성격이므로 system이 맞고,
human 템플릿에 넣으면 매 턴 컨텍스트 블록 사이에 규칙이 섞여 가독성과 캐시 재사용이 나빠진다.
검토했으나 채택하지 않은 대안:
- *프롬프트 빌더를 언어별로 분기* — 프롬프트 본문이 언어 수만큼 복제되어 유지보수가 배로 늘어난다.
- *출력 스키마에 `language` 필드 추가* — 모델이 "선언"만 하고 본문은 영어로 쓰는 실패 모드가 남는다.
- *human 템플릿 말미 리마인더* — 지시 준수가 약할 때의 **fallback**으로만 남긴다(아래 Step 2 참고).

**결정 6 — 지시문 자체를 대상 언어로 작성한다.**
`ko`면 언어 지시문이 한국어로, `en`이면 영문으로 들어간다. 즉 `LANGUAGE_DIRECTIVE`는
`{언어명}`을 끼워 넣는 하나의 영문 템플릿이 아니라, **언어별로 통째로 작성된 상수 2개**다.

```python
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: "모든 자연어 값을 한국어로 작성한다. JSON 키와 step 번호는 그대로 둔다. ...",
    OutputLanguage.en: "Write every natural-language value in English. Keep JSON keys and step numbers as-is. ...",
}
```

근거: 지시문이 쓰인 언어 자체가 출력 언어에 대한 가장 강한 신호다. 영문 지시문으로 "한국어로 써라"라고
말하는 것보다, 한국어로 쓰인 지시문이 한국어 출력을 훨씬 안정적으로 끌어낸다 — 특히 결과가
`with_structured_output`으로 강제되어 모델이 형식에 주의를 뺏기는 상황에서 차이가 난다.

비용: `SYSTEM_PROMPT`의 나머지(역할·draft 처리 규칙)는 영문이라 `ko`일 때 한·영이 섞인다. 실무상 문제되지
않고, 언어별로 시스템 프롬프트 전체를 복제하는 것보다 유지보수 비용이 훨씬 낮다.
언어가 3개 이상으로 늘면 상수가 언어 수만큼 늘어나는데, 그건 지시문 한 문단 분량이라 감당 가능한 선이다.

**결정 5 — `state` / `action` / `expected`는 코드 식별자 없는 순수 자연어로 쓴다.**
`unity_context` / `game_context`의 GameObject 이름·컴포넌트/메서드명·에셋 경로를 문장에 그대로 박지 않는다.
`BuyButton을 누른다`가 아니라 `구매 버튼을 누른다`, `ShopScene을 로드한다`가 아니라 `상점 화면에 진입한다`.
컨텍스트는 **무엇을 테스트할지 파악하는 근거**로만 쓰고, 출력 문장은 그 의도를 사람 언어로 서술한다.

근거는 위의 QA Execution Agent 분리다. 식별자를 시나리오에 박아 두면 (1) 씬 구조가 바뀔 때마다 시나리오가
통째로 낡고, (2) 실행 에이전트가 런타임에 실제 invoke 가능한 함수를 보고 고르는 여지를 미리 닫아 버린다.
게다가 이 요구는 언어 지정과 정면으로 맞물린다 — 식별자를 문장에 섞도록 두면 모델이 그 주변 문장까지
영어로 남기는 쪽으로 새서, 애초에 원하는 "지정 언어로 된 draft"가 반쯤 깨진다.

이건 언어 지정을 넘어선 **출력 내용 자체의 정책 변경**이다. 현재 `SYSTEM_PROMPT`/`OUTPUT_CONTRACT`는
식별자 사용을 금지하지도 권장하지도 않아 모델 재량에 맡겨져 있다. 언어 지시문과 같은 자리에서 함께
못 박는 게 자연스러워 이번 변경에 포함하되, 커밋은 분리한다(언어 파라미터 배선 / 프롬프트 정책 강화).

### 체크리스트

- [ ] **Step 0: Recon** — 위 관련 파일 재확인, `model` 파라미터가 지나가는 모든 지점을 grep(`model=`, `LLMModel`)으로 확정.

- [ ] **Step 1: Implementation**
  - [ ] `app/agents/scenario/schemas.py`: `OutputLanguage(StrEnum)`(`ko`, `en`) + `DEFAULT_LANGUAGE = ko` 추가,
        `ScenarioAgentRequest.language: OutputLanguage = DEFAULT_LANGUAGE` 필드 추가.
  - [ ] `app/agents/scenario/prompt.py`: `LANGUAGE_DIRECTIVES: dict[OutputLanguage, str]` 추가
        (결정 6 — 언어별 전문 상수 2개, 각각 해당 언어로 작성). `SYSTEM_PROMPT`에 `{language_directive}`
        자리를 두고 `build_chain_inputs`가 `LANGUAGE_DIRECTIVES[request.language]`로 채운다.
        누락 시 조용히 영어로 새지 않도록 `dict[...]` 직접 접근(KeyError)으로 두고 `.get(..., default)`는 쓰지 않는다.
  - [ ] `app/agents/scenario/prompt.py` (별도 커밋): 식별자 금지 규칙을 `SYSTEM_PROMPT`에 고정 문장으로 추가.
        골자: "Describe `state`, `action`, and `expected` as plain natural language a human tester
        would use — do not embed code identifiers (GameObject names, component or method names,
        scene names, asset paths) from the provided context. Say `구매 버튼을 누른다`, not
        `BuyButton을 누른다`. Binding these intents to invokable functions happens later, at execution time."
        `OUTPUT_CONTRACT`의 `action` 설명도 같은 취지로 다듬는다(현재: "Concrete action the Unity game
        QA tester should perform").
  - [ ] `app/agents/scenario/__init__.py`: `OutputLanguage`, `DEFAULT_LANGUAGE` re-export.
  - [ ] `app/sessions/schemas.py`: `SessionRecord.language` (default 포함 — 역호환 필수).
  - [ ] `app/sessions/service.py`: `open(..., language=DEFAULT_LANGUAGE)`,
        `run_turn(..., language: OutputLanguage | None = None)`에서 `model`과 동일하게 override 후 저장,
        `_generate`가 `record.language`를 `ScenarioAgentRequest`에 전달.
  - [ ] `app/api/sessions.py`: `OpenSessionRequest.language`(default `ko`),
        `TurnMessage.language: OutputLanguage | None = None` 추가 및 서비스 호출에 전달.
  - [x] ~~`docs/api-documentation.md`~~ — 이 문서는 엔드포인트를 수기로 나열하지 않고 "실행 중인 앱의
        OpenAPI JSON이 곧 계약"이라고 명시한다. `language`는 Pydantic 모델 필드라 OpenAPI에 자동 반영되므로
        수기 문서 편집 없음. `/extract` 비적용은 플랜 Non-goals로 갈음.

- [ ] **Step 2: Tests** (`tests/test_agents_scenario.py`, `tests/test_sessions.py`, `tests/test_api.py`)
  - [ ] `build_chain_inputs`가 요청 언어에 맞는 지시문을 담는다 — `ko`면 한국어 지시문, `en`이면 영문 지시문.
  - [ ] 기본값 미지정 시 `ko` 지시문이 들어간다.
  - [ ] `LANGUAGE_DIRECTIVES`가 `OutputLanguage`의 모든 멤버를 덮는다 (enum 추가 시 상수 누락을 잡는 가드).
  - [ ] `POST /sessions`에 `language: "en"`으로 연 세션의 **첫 턴**(`start_first_turn`)이 영문 지시문을 탄다
        — 결정 3에서 짚은 first-turn 누락 회귀를 고정한다.
  - [ ] `SessionRecord`를 `language` 키 **없는** JSON에서 역직렬화해도 `ko`로 로드된다(역호환 회귀 테스트).
  - [ ] WS `turn`에 `language`를 실으면 레코드가 갱신되고 이후 턴에도 유지된다.
  - [ ] 잘못된 값(`"korean"`, `"ja"`)은 `POST /sessions` 422, WS turn은 기존 `bad_request` 에러 이벤트로 떨어진다.
  - [ ] 수동 검증(실제 모델 1회 호출, `ko`): 식별자가 많이 담긴 `unity_context`를 일부러 넣고
        (a) Goal의 7개 필드가 **모두** 한국어인지, (b) `state`/`action`/`expected`에 코드 식별자가
        새어 나오지 않았는지 확인. `message`와 `steps[*].expected`가 가장 잘 새는 지점이므로 여기를 먼저 본다.
        준수가 약하면 그때 `HUMAN_TEMPLATE` 말미에 한 줄 리마인더를 추가한다(결정 4의 fallback).
  - [ ] 식별자 금지는 프롬프트 지시일 뿐 스키마로 강제되지 않는다. 자동 테스트로는 검증할 수 없으므로
        (실제 모델 호출 없이는 재현 불가) 수동 검증 항목으로만 남긴다 — 이 한계를 PR 본문에 명시한다.

- [ ] **Step 3: Rollout / Rollback** — 별도 플래그 없음. 모든 신규 필드가 default를 가지므로
      기존 클라이언트는 무변경으로 동작하고 배포는 무중단이다.

## Validation

- **Commands to run:**
  ```bash
  pytest && ruff check app tests
  ```
- **Expected output:** 전체 통과. 신규 테스트 5~6개 추가, 기존 세션/시나리오 테스트는 무수정 통과
  (기존 호출부가 모두 default를 타므로 시그니처 변경이 기존 테스트를 깨지 않아야 한다 — 깨진다면 default 누락 신호).

## Risks & Rollback

- **Risks:**
  - 모델이 언어 지시를 부분적으로만 따라 `message`는 한국어, `steps[*].expected`는 영어로 섞일 수 있다.
    → 지시문에서 "every natural-language value"를 명시하고, 미준수 시 human 리마인더 fallback.
  - 식별자 금지 규칙이 과하게 작동해 문장이 모호해질 수 있다("버튼을 누른다" — 어떤 버튼인지 불명).
    실행 에이전트는 후보 목록 안에서 고르므로 절대적 정밀함까지는 필요 없지만, **후보들 사이에서
    구별 가능한** 수준은 유지해야 한다. 지시문은 "식별자를 쓰지 마라"가 아니라 "사람이 화면을 보고
    지칭하는 방식으로 쓰라"에 무게를 둔다. 수동 검증 대상.
  - 지정 언어(한국어) 문장과 씬 후보 이름(대개 영어 식별자) 사이의 **교차 언어 매칭**이 실행 시점에 발생한다
    (`구매 버튼` ↔ `OnBuyButtonClicked`). LLM이 통상 잘 처리하는 범위라 낮은 위험으로 보되,
    실행 에이전트 구현 시 확인 항목으로 넘긴다.
  - 식별자 금지는 기존 출력 스타일의 변경이다. 이전 시나리오와 톤이 달라지므로, 저장된 draft를 이어받아
    수정하는 턴에서 신·구 스타일이 한 시나리오 안에 섞일 수 있다. 치명적이지 않아 수용하되 PR에 명시.
  - Redis에 남아 있는 기존 세션 레코드 역직렬화. default로 해소되며 회귀 테스트로 고정.
- **Rollback steps:** 단일 커밋 `git revert`. 저장 포맷은 additive라 롤백 후에도 기존 레코드가 그대로 읽힌다.

## Open Questions

- ~~초기 지원 언어 범위~~ — 확정: `ko` / `en` 2종.
- ~~`/extract` 적용 여부~~ — 확정: 비적용, 영문 고정.
- ~~기본값을 `Settings`로 뺄지~~ — 확정: 코드 상수 `DEFAULT_LANGUAGE = ko`. 실제 기본값은 DB 칼럼이 갖고,
  이쪽은 키 누락 방어선일 뿐이라 환경별로 달라질 이유가 없다.
- ~~오케스트레이션이 사용자 로케일을 아는지~~ — 확정: `user.language` 칼럼(default `ko`)에서 읽어 넘긴다.
- 오케스트레이션 쪽 작업 순서. 이 서버가 먼저 나가도 `language` 기본값 `ko`로 기존 동작을 유지하므로
  배포 순서 제약은 없다. 다만 칼럼 추가 전까지는 항상 `ko`로 동작한다는 점을 양쪽이 합의해야 한다.
- 사용자가 언어를 바꿨을 때 **진행 중인 세션**에 소급 적용할지. 지금 설계로는 WS `turn`에 새 값을
  실어 보내면 그 턴부터 바뀌지만(이미 생성된 draft는 그대로), 그 트리거를 오케스트레이션이 걸어줄지는
  미정. 걸지 않으면 다음 세션부터 반영된다 — 이쪽이 기본 동작이고 대부분 충분해 보인다.
- ~~자연어 문장의 구체성 하한선~~ — 해소됨. step마다 씬 후보(state/event/function)가 주입되므로
  요구 조건은 "절대적 정밀함"이 아니라 "후보들 사이에서 구별 가능"이다. 프롬프트에는 좋은 예/나쁜 예
  한 쌍만 넣고, 실행 에이전트 구현 후 실제 매칭 실패 사례가 나오면 그때 역으로 조인다.
- `state`의 성격. "타이틀 화면"처럼 화면 상태를 쓰는 것과 "골드를 100 이상 보유"처럼 데이터 조건을 쓰는 것이
  섞여 있다(현 `OUTPUT_CONTRACT` 설명이 둘 다 예시로 든다). 실행 시점에 전자는 씬/화면 확인, 후자는
  state 값 조회로 검증 경로가 갈리므로 실행 에이전트 설계에서 구분이 필요할 수 있다.
  이번 변경에서는 손대지 않고 넘긴다.
- 씬 후보 정보(state/event/function)를 **누가 어느 시점에 수집**하는지 — Unity SDK가 런타임에 밀어주는지,
  오케스트레이션이 미리 모아 두는지. 이 서버의 이번 변경 범위 밖이지만, `unity_context`의 향후 스키마와
  겹칠 가능성이 있어 실행 에이전트 설계 착수 전에 정리가 필요하다.
