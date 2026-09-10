# 2026-09-10 — 기본 model 을 DEFAULT_MODEL 환경변수로 정한다

- Date: 2026-09-10
- Jira: ARTEL-884
- Status: Implemented, pair-reviewed, tests green

## Goal

이 배포가 어느 model 을 기본으로 쓰는지를 `.env` 로 정하게 한다. 지금은
`app/llm/models.py` 의 모듈 상수 `DEFAULT_MODEL` 하나가 그것을 정하고, 값이
`openai/gpt-5.6-luna` 라는 OpenRouter slug 라, model 을 명시하지 않는 호출은
전부 OpenRouter 로 나간다.

`POST /extract` 가 그 경로다. orchestration 의 `artel.agent.extract.model` 이
비어 있으면 (`application.yml` 의 기본값) `AgentExtractClient` 가 `model` 필드를
빼고 보내고, `ExtractRequest.model` 이 `DEFAULT_MODEL` 로 채워진다. 로컬에서
OpenRouter 잔액이 0 이라 (`GET /credits`: `total_credits` 200, `total_usage`
200.15) 기획 문서 지식 추출이 죽었다. 같은 stack 의 QA 런은 요청에 Bedrock slug
를 실어 보내므로 멀쩡히 돈다.

같은 실패를 한 번 겪었다. `qa_compaction_model` 이 OpenRouter slug 로 고정돼
있어서 런은 Bedrock 으로 도는데 압축만 OpenRouter 로 나갔고, 잔액이 없을 때
조용히 실패했다 (ARTEL-776). 그때는 압축이 런의 model 을 따라가게 고쳤다. 남은
것이 기본값 자체다.

catalog 에 이미 있는 `bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` 이
이 변경이 가리킬 대상이다. Bedrock 에서 고를 수 있는 model 을 늘리는 일은 여기
안 들어간다 — Non-goals 를 보라.

## Non-goals

- orchestration 의 `artel.agent.extract.model` 은 건드리지 않는다. 배포 전체의
  기본값과 추출만의 model 은 다른 축이고 둘 다 남는다. 그쪽은 이 변경이 들어간
  뒤에도 특정 model 로 추출을 고정하고 싶을 때 쓰는 자리다.
- `DEFAULT_MODEL` 상수의 값을 바꾸지 않는다. `openai/gpt-5.6-luna` 그대로 두고,
  정하는 자리만 늘린다. 설정을 안 준 배포는 종전과 똑같이 돈다.
- `list_models` 가 돌려주는 catalog 에 "이것이 기본" 표시를 넣지 않는다. 지금
  그 응답에 그런 필드가 없고, 프런트가 기본을 고르는 화면은 이 이슈의 범위가
  아니다.
- **Bedrock catalog 에 model 을 더하지 않는다.** 처음에는 Claude 3.7 Sonnet,
  3.5 Sonnet, 3 Opus 세 항목을 같이 넣으려 했다. Bedrock control plane 에 물어
  보니 셋 다 이 계정 `us-west-2` 에 없다 — foundation model 목록에도,
  `ACTIVE` inference profile 73 개 중에도 없다. Claude 3 세대에서 남은 것은
  `claude-3-haiku-20240307-v1:0` 하나다. catalog 에 넣으면 뜨기는 하지만 고르는
  순간 Bedrock 이 거절하므로 뺀다. 넣는다면 살아 있는 `us.anthropic.claude-sonnet-5`
  나 `us.anthropic.claude-opus-5` 여야 하고, 그것은 따로 정할 일이다.
- `EMBEDDING_MODEL` 은 이미 설정으로 정할 수 있고 이 변경과 무관하다.
- `qa_compaction_model` 의 동작을 바꾸지 않는다. 비어 있으면 런의 model 을
  따르는 그 규칙 그대로다.

## Context / Constraints

**`app/config.py` 는 `app.llm` 을 module 수준에서 import 할 수 없다.**
`app.llm.__init__` 이 `chat_model` 을 끌어오고 그것이 `app.config` 를 다시
부른다. `qa_compaction_model` 이 `LLMModel` 이 아니라 `str | None` 인 이유가
그것이고, 그 위 주석이 이유를 적어 두었다. 새 항목도 같은 처리를 따른다 —
`str | None` 으로 받고, validator 안에서 `LLMModel` 을 늦게 import 해서 catalog
에 있는지 본다.

**반대 방향은 열려 있다.** `app/config.py` 는 module 수준에서 pydantic 말고 아무
것도 import 하지 않으므로, `app/llm` 아래 module 이 `from app.config import
get_settings` 를 맨 위에 두어도 순환이 안 생긴다. `app/llm/chat_model.py` 가 이미
그렇게 한다.

**resolver 는 `models.py` 가 아니라 새 module 에 둔다.** `models.py` 는 지금
바깥 import 가 하나도 없는 값만 있는 catalog 이고, 거기에 `Settings` 의존을
넣으면 그 성격이 바뀐다. 그래서 `app/llm/default_model.py` 를 만들고 상수와
resolver 를 함께 둔다. `models.py` 는 catalog 로 남고, "어느 것이 기본이고
어떻게 정해지나" 는 한 파일이 답한다.

import 무게는 이 결정의 근거가 아니다. `app.llm` 아래 무엇을 import 하든
`app/llm/__init__.py` 가 먼저 돌고 그것이 `chat_model` 을 끌어오므로,
`langchain_aws` 와 `langchain_openai` 는 이미 11 개 schema module 전부에
딸려 온다 (`import app.sessions.schemas` 가 0.92 초, `boto3` 포함). module 을
나눠도 그것은 안 변한다.

**상수 이름을 `FALLBACK_MODEL` 로 바꾼다.** 환경변수 `DEFAULT_MODEL`, 설정 항목
`Settings.default_model`, 상수 `DEFAULT_MODEL` 이 셋 다 같은 문자열로 검색되면,
"상수는 기본이 아니라 못 정했을 때의 값" 이라는 사실을 읽는 사람이 외워야 한다.
이름이 그것을 말하게 한다. 환경변수 이름과 설정 항목 이름은 그대로 둔다 — 그
둘은 정말로 기본을 정하는 자리다.

**Pydantic 은 class 가 정의될 때 field default 를 한 번 읽는다.** 그래서
`model: LLMModel = DEFAULT_MODEL` 을 그대로 두면 설정을 바꿔도 안 따라간다.
`Field(default_factory=resolve_default_model)` 로 바꿔야 인스턴스를 만들 때마다
설정을 본다.

**기본값을 쓰는 자리는 17 곳이고 두 종류다.**

Pydantic field 11 곳 — `Field(default_factory=resolve_default_model)` 로 바꾼다:

| 파일 | 줄 | class |
|---|---|---|
| `app/api/extract.py` | 19 | `ExtractRequest` |
| `app/api/qa_sessions.py` | 77 | `OpenQaSessionRequest` |
| `app/api/sessions.py` | 43 | `OpenSessionRequest` |
| `app/api/knowledge_queries.py` | 23 | `KnowledgeQueriesRequest` |
| `app/api/step_phrasing.py` | 34 | `StepPhrasingBody` |
| `app/sessions/schemas.py` | 42 | `SessionRecord` |
| `app/agents/game_context/schemas.py` | 82 | `GameContextAgentRequest` |
| `app/agents/knowledge_query/schemas.py` | 44 | `KnowledgeQueryAgentRequest` |
| `app/agents/screen_verdict/schemas.py` | 53 | `ScreenVerdictRequest` |
| `app/agents/scenario/schemas.py` | 167 | `ScenarioAgentRequest` |
| `app/agents/step_phrasing/schemas.py` | 54 | `StepPhrasingRequest` |

함수·method 인자 6 곳 — `LLMModel | None = None` 으로 받고 안에서 채운다:

| 파일 | 줄 | 대상 |
|---|---|---|
| `app/documents/service.py` | 41 | `ExtractionService.extract` |
| `app/sessions/service.py` | 44 | `SessionService.open` |
| `app/qa/service.py` | 78 | `QaSessionService.open` |
| `app/qa/run_config.py` | 110 | `resolve_run_config` |
| `app/qa/screen_verdict.py` | 88 | `ScreenSelectorAdjudicator.__init__` |
| `app/agents/knowledge_query/agent.py` | 90 | `KnowledgeQueryAgent.run_batch` |

두 번째 표의 6 곳은 실행 경로에서는 거의 안 쓰인다 — API 층이 요청 schema 에서
이미 정해진 값을 늘 넘긴다. 그래도 같이 바꾼다. 한쪽만 설정을 보면 두 기본값이
서로 다른 답을 내고, 그것이 지금 고치는 실패와 같은 종류다. 테스트는 실제로 이
인자를 비운 채 부른다 (`resolve_run_config(arch=...)`,
`agent.run_batch(items, _CTX)`).

**suite 전체가 개발자 `.env` 에 매인다. `tests/conftest.py` 를 새로 만든다.**
`tests/test_qa_run_config_contract.py`, `test_qa_arch.py`, `test_qa_prompt_version.py`
는 `TestClient(app)` 으로 실제 `.env` 를 읽는다. `DEFAULT_MODEL` 을 적어 둔
기계에서는 단정 하나가 아니라 여럿이 깨진다 — 47 줄의 `config["model"]` 뿐
아니라 48 줄의 `config["provider"] == "openai"` 가 Bedrock 기본값에서
`anthropic` 이 되고, Bedrock 항목이 `reasoning_default_tokens` 를 들고 있으면
`run_config["reasoning"]` 도 더 이상 null 이 아니다.

그래서 한 줄 고치기로는 안 된다. `tests/conftest.py` (지금 없다) 에서 `app` 을
import 하기 전에 `os.environ["DEFAULT_MODEL"] = ""` 을 박는다. `os.environ` 이
`_env_file` 을 이기므로 이것이 확실한 자리다. 그러고 나서
`tests/test_qa_run_config_contract.py:47` 의 `DEFAULT_MODEL.value` 를
`FALLBACK_MODEL.value` 로 바꾼다.

**테스트는 `get_settings()` 의 `lru_cache` 를 건드리지 않는다.** 이 repository
에 이미 있는 방식을 따른다 (`tests/test_llm_bedrock_credentials.py:41`):
`Settings(_env_file=None, ...)` 를 직접 만들고
`monkeypatch.setattr("app.llm.default_model.get_settings", lambda: settings)`
로 그 module 이 보는 자리만 바꾼다. 캐시를 비우는 방식은 같은 프로세스의 뒤
테스트를 오염시킨다.

**이 기계의 shell 이 `OPENROUTER_API_KEY` 를 export 한다** (`~/.zshenv:10`).
`os.environ` 이 `_env_file` 을 이긴다는 뜻이라, `tests/test_config.py` 의
`test_settings_can_load_from_env_file` 과 `test_the_new_name_wins_over_the_old_one`
두 개가 이 변경 전부터 이 기계에서 깨져 있다. 고치는 것은 이 이슈 밖이지만, 새로
쓰는 테스트가 같은 함정에 안 빠지게 `monkeypatch.delenv("DEFAULT_MODEL",
raising=False)` 를 쓴다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 위 두 표의 17 곳과 `tests/test_config.py`,
  `tests/test_qa_run_config_contract.py` 를 확인했다. `list_models` 는 기본을
  안 알리므로 손댈 것이 없다.

- [x] **Step 1: Core** — 설정 항목과 resolver
  - `app/config.py`: `llm_base_url` 아래에 `default_model: str | None = None`
    을 둔다. 주석에 적을 것 — 비우면 `FALLBACK_MODEL` 을 따른다는 것, 값이
    `bedrock/` 로 시작하면 그 배포의 기본 호출이 Bedrock 으로 간다는 것, 그리고
    `LLMModel` 로 타입을 못 붙이는 이유 (`qa_compaction_model` 과 같은 순환).
  - `app/config.py`: 기존 `known_model` validator 를
    `@field_validator("qa_compaction_model", "default_model")` 로 넓히고, 오류
    문구의 필드 이름을 `ValidationInfo.field_name` 에서 가져온다. 이름은
    `known_model` 그대로 둔다 — 두 항목에 같은 질문("catalog 에 있나")을 하므로
    이름이 여전히 맞는다.
  - `app/llm/default_model.py` (신규): `FALLBACK_MODEL: LLMModel =
    LLMModel.gpt_5_6_luna` 와 `resolve_default_model() -> LLMModel`. 설정이
    비었으면 `FALLBACK_MODEL`, 있으면 `LLMModel(value)`. 함수 docstring 이 이
    변경의 근거를 한 곳에 적는다 — 호출부 17 곳이 이 문서를 가리킨다.
  - `app/llm/models.py`: `DEFAULT_MODEL` 을 뺀다. catalog 로 남는다.
  - `app/llm/__init__.py`: `DEFAULT_MODEL` re-export 를 `FALLBACK_MODEL` 과
    `resolve_default_model` 로 바꾸고 `__all__` 을 고친다.
  - `.env.example`: `OPENROUTER_BASE_URL` 아래에 `DEFAULT_MODEL=""` 과 설명.

- [x] **Step 2: 호출부 17 곳** — 위 두 표대로 바꾼다. Pydantic field 는
  `Field(default_factory=resolve_default_model)`, 함수 인자는
  `LLMModel | None = None` 뒤에 `model or resolve_default_model()`. 호출부에는
  주석을 안 단다 — 같은 문장을 17 번 쓰는 대신 `resolve_default_model()` 의
  docstring 하나가 근거를 든다.

- [x] **Step 3: Tests**
  - `tests/test_config.py`: 비우면 `None` 인 것, catalog 밖 값이 거절되는 것.
  - `tests/test_llm_default_model.py` (신규): 설정이 비면 `FALLBACK_MODEL`,
    Bedrock slug 를 주면 그것, 그리고 **요청 schema 의 기본값이 설정을 따라간다**
    는 것. 마지막 단정이 이 이슈의 핵심이다 — `default_factory` 없이 상수
    default 로 두면 이 테스트만 깨진다. 함수 인자 6 곳 중 하나
    (`resolve_run_config`) 도 같은 방식으로 확인한다.
  - `tests/test_qa_run_config_contract.py:47`: `resolve_default_model().value`
    로 바꾼다.
  - `tests/conftest.py` (신규): `app` import 보다 먼저
    `os.environ["DEFAULT_MODEL"] = ""`.
  - `SessionRecord` round-trip: Redis 에 넣었다 뺀 record 가 저장될 때의 model 을
    그대로 들고 나오는지. 지금은 안전하다 —
    `app/sessions/redis_store.py:19` 와 `store.py:27` 이 `model_dump_json()` 을
    옵션 없이 부르므로 `model` 이 늘 실려 나가고 읽을 때 `default_factory` 가 안
    돈다. 나중에 누가 `exclude_defaults=True` 를 붙이면 그 순간 저장된 session 이
    현재 환경변수를 따라가는 버그가 되므로, 그것을 잡는 단정을 남긴다.
  - 방식은 `Settings(_env_file=None, ...)` + `monkeypatch.setattr` 로 고정한다.
    위 Context 의 두 문단이 이유다. `resolve_default_model` 자체를 patch 하면
    안 먹는다 — `default_factory` 가 함수 객체를 잡아 두기 때문이다.
    `app.llm.default_model.get_settings` 를 patch 해야 한다.

## Validation

- **Commands to run:**
  - `LANGSMITH_TRACING=false python -m pytest`
  - `LANGSMITH_TRACING=false python -m pytest tests/test_config.py tests/test_llm_default_model.py tests/test_qa_run_config_contract.py -v`
- **Expected output:** 전부 통과. `LANGSMITH_TRACING=false` 를 안 주면 429 로그가
  결과를 덮는다.

**결과 (2026-09-10).** 전체 suite `925 passed, 2 failed`. 기준선은 이 변경 전
`917 passed, 2 failed` 였고 실패한 둘은 같은 것이다 —
`test_settings_can_load_from_env_file` 와 `test_the_new_name_wins_over_the_old_one`
가 `~/.zshenv:10` 의 `OPENROUTER_API_KEY` export 때문에 이 기계에서 깨진다. 이
변경과 무관하고 고치지 않았다. 늘어난 8 개가 새로 쓴 테스트다.

**세 경로를 직접 확인했다.**

| 준 값 | 요청 기본값 | 지어진 client |
|---|---|---|
| `DEFAULT_MODEL=bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` | 같은 값 | `ChatBedrockConverse`, `us-west-2` |
| 안 줌 | `openai/gpt-5.6-luna` | `ChatOpenAI` |
| `DEFAULT_MODEL=anthropic/nope` | — | `get_settings()` 가 거절: `default_model 'anthropic/nope' is not in the catalog. Known: ...` |

가운데 줄이 되돌림 경로다. 설정을 안 준 배포는 종전과 같은 model, 같은 client 다.
- 수동: `DEFAULT_MODEL=bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` 을
  `.env` 에 두고 서버를 띄워 `POST /internal/extract` 를 `model` 없이 불러,
  Bedrock 으로 나가 성공하는지 본다. 이것이 이슈를 만든 실패의 재현이다.

## Risks & Rollback

- **Risks:**
  - `Field(default_factory=...)` 로 바꾸면 그 field 가 OpenAPI schema 에서
    고정 default 를 잃는다. Insomnia collection 과 `/openapi.json` 을 읽는 쪽에
    보이는 변화라, 실제 응답을 확인한다.
  - `app/llm/default_model.py` 가 `app/config.py` 를 import 하므로, 나중에 누가
    `config.py` 맨 위에 `app.llm` import 를 넣으면 순환이 생긴다. 지금도 그것을
    금지하는 주석이 있고, 새 항목 주석에도 같은 이유를 적는다.
  - `DEFAULT_MODEL` 상수를 `FALLBACK_MODEL` 로 바꾸므로, 그 이름을 쓰는 곳이
    전부 같이 바뀌어야 한다. `app/` 안에는 없고 (`app/llm/__init__.py` 의
    re-export 와 호출부 17 곳뿐), `tests/` 에는 `test_qa_run_config_contract.py`
    와 `test_qa_model_reasoning.py` 의 주석 한 줄이 있다.
  - 잘못된 `DEFAULT_MODEL` 을 준 배포는 첫 `get_settings()` 에서 죽는다. 이것은
    의도한 것이다 — 조용히 다른 model 로 도는 것보다 낫고, `qa_compaction_model`
    이 이미 같은 규칙이다.
  - 설정을 안 준 배포는 종전과 완전히 같다. 그것이 이 변경의 되돌림 경로이기도
    하다.
- **Rollback steps:** `git revert`. 설정을 안 준 배포에는 동작 변화가 없으므로
  되돌려도 잃는 것이 없다. 급하면 `.env` 에서 `DEFAULT_MODEL` 을 비우는 것만으로
  종전 동작으로 돌아간다.

## Pair review

첫 판정은 `NONPASS` 였다. must-fix 하나와 should-fix 일곱을 받았고, 여섯을 고쳤다.

**must-fix — `test_a_stored_session_keeps_the_model_it_was_opened_with` 이
tautology 였다.** 기본값과 다른 model 을 저장했기 때문에 `exclude_defaults=True` 가
붙어도 field 가 실려 나가 단정이 통과했다. docstring 이 지킨다고 적은 것을 안
지키면서 초록불을 주는, 없는 것보다 나쁜 상태였다. `InMemorySessionStore` 를 실제로
통과시키고 저장하는 값을 `FALLBACK_MODEL` 로 바꿨다.

**should-fix — `app/qa/service.py` 의 `model = model or resolve_default_model()`
는 죽은 줄이었다.** 그 method 에서 `model` 의 유일한 사용처가 바로 아래
`resolve_run_config(model=model, ...)` 이고 거기가 같은 해석을 다시 한다. 17 곳을
다 바꾼 근거("한쪽만 설정을 보면 두 기본값이 다른 답을 낸다")가 여기엔 안 걸린다 —
이 method 는 자기 기본값이 없고 위임만 한다. signature 의 `LLMModel | None = None`
은 남기고 body 한 줄과 import 를 지웠다. 남은 다섯 곳은 `model` 을 뒤에서 실제로
쓰므로 그대로다.

**should-fix — Pydantic field 11 곳 중 `ExtractRequest` 하나만 덮여 있었다.**
`test_every_request_schema_reads_the_setting` 을 더해 11 개 class 의
`default_factory` 를 pin 했다. wiring 만 보는 약한 단정이지만, 없으면 그 wiring 을
보는 것이 하나도 없었다.

나머지 셋은 말 고르기였다 — `tests/conftest.py` docstring 을 한국어로 (같은 변경의
다른 새 파일 셋은 한국어인데 이것만 영어였다), `창` 을 `context window` 로,
`되돌림 경로` 를 `rollback 경로` 로, `안 쥔 배포` 를 `설정하지 않은 배포` 로.

**두 테스트가 정말 regression 을 잡는지 mutation 으로 확인했다.**
`InMemorySessionStore.save` 에 `exclude_defaults=True` 를 붙이고
`ExtractRequest.model` 을 상수 default 로 되돌렸더니 세 테스트가 깨졌고
(`..._stored_session...`, `..._request_naming_no_model...`,
`..._every_request_schema...`), 되돌리니 다시 통과했다.

### 안 고친 것

- **`app/llm/__init__.py` 의 `FALLBACK_MODEL` · `resolve_default_model`
  re-export 를 쓰는 곳이 없다.** 종전 `DEFAULT_MODEL` re-export 도 마찬가지였으므로
  새로 생긴 빚이 아니고, 지우는 것은 이 변경과 무관한 정리다. package 의 공개
  표면을 종전과 같은 모양으로 둔다.

## Rejected feedback

- **호출부 6 곳의 함수 인자는 그대로 두자** (medium reviewer 가 양쪽을 재고
  결국 바꾸는 쪽을 권했다). 안 바꾸면 안쪽 기본값은 상수를 보고 바깥 schema 는
  설정을 봐서 두 기본값이 서로 다른 답을 낸다. 그것이 ARTEL-776 에서 이미 한 번
  일어난 실패다. 바꾼다.
- **Bedrock catalog 를 아예 다른 PR 로 빼자.** 결과적으로 그보다 더 나갔다.
  heavy review 가 요구한 대로 실제 계정에 확인했더니 세 항목이 존재하지 않는
  model 이었고, 사용자가 빼기로 정했다. Non-goals 에 근거를 남겼다.

## Open Questions

- 없음. `list_models` 에 기본 표시를 넣을지는 Non-goals 로 잘라 뒀다.
