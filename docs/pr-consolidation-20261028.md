# agent-server PR 11개 통합 기록

2026-10-28 기준 `dev-yunseong`의 아래 열린 PR을 `develop` 기반 통합 branch에 merge했다. 원본 branch와 작성자 commit은 수정하지 않으며, 각 source head의 ancestry를 검증한다. 다른 작성자의 PR 상태는 변경하지 않는다.

| 원본 PR | 변경 | 고정한 head |
| --- | --- | --- |
| [#181](https://github.com/project-artel/artel-agent-server/pull/181) | feat(agent): 기본 model 을 DEFAULT_MODEL 환경변수로 정한다 | `3fb1512a08abe71a0cdfb1a028ffa77592cdd464` |
| [#257](https://github.com/project-artel/artel-agent-server/pull/257) | fix: planner 설명과 검증 결과 출력 언어 적용 | `89b9ea36a1f14ae1ce9c9c172acd5d1dc4214fb0` |
| [#261](https://github.com/project-artel/artel-agent-server/pull/261) | fix(qa): QaArchSpec 의 vision 에 boolean 수용 | `443719e2e655c0b42f51b73ea621a14b1c29529f` |
| [#262](https://github.com/project-artel/artel-agent-server/pull/262) | fix(qa): report_step 판정을 기대값과 관찰값 대조로 변경 | `02ab5f651851452c03d820efeebe6c504f2a2cd7` |
| [#263](https://github.com/project-artel/artel-agent-server/pull/263) | feat(qa): tool 결과에 안 본 예외 수 한 줄과 get_errors tool 추가 | `2c3984a6bc797622107eb1149b384f8a6e68b477` |
| [#264](https://github.com/project-artel/artel-agent-server/pull/264) | feat(qa): screen capture 답에 시각 이상 점검 목록과 scene 첫 진입 capture 요청 추가 | `3e76e30cf7cb610ce259a04ea64cd326d8a47629` |
| [#265](https://github.com/project-artel/artel-agent-server/pull/265) | fix(qa): macro 답에 macro 가 도는 동안 뜬 글자 목록 추가 | `ec48a843a5f8cb721c3049bc4860d2abc045e357` |
| [#266](https://github.com/project-artel/artel-agent-server/pull/266) | feat(qa): get_performance tool 과 지난 10초 hitch 수 한 줄 추가 | `6e3e7e22e5051d70bff24e58047e6296e79f03d8` |
| [#267](https://github.com/project-artel/artel-agent-server/pull/267) | fix(llm): 기능별 model 설정에서 effective 값 읽기 | `e0c1641ff564e24fc6494f0b4a29418f95872fd3` |
| [#268](https://github.com/project-artel/artel-agent-server/pull/268) | feat(agent): 시연 검증 절차 추출과 QA context 추가 | `3c3e765c2c3fc73940f4efd29e7db60c85fd95fd` |
| [#275](https://github.com/project-artel/artel-agent-server/pull/275) | feat(qa): TC 검증 시도와 사용자 질문 대기 추가 | `fa30d69fa3c305253a3b2098365ffc45bffa1e89` |

## 충돌 해결

- #181의 `DEFAULT_MODEL` 환경변수 선택을 최신 원격 DB model catalog에 연결했다. 정적 Enum/catalog는 복구하지 않았으며, 요청의 `EnabledLLMModel`, reasoning, provider key와 기능별 effective model 설정을 유지했다. 기본 model이 catalog 밖이거나 꺼져 있으면 실제 선택 시 설정 이름을 포함해 거절한다. 원격 catalog를 읽기 전인 Settings 생성에서는 검증하지 않는다.
- #257의 planner 출력 언어와 #268의 승인된 시연 절차를 함께 유지한다. planner label은 `pe4-language-demonstration-procedures`다.
- #262~#266의 기대값 대조·예외·성능 조회·첫 scene capture·macro 도중 표시한 text 점검과 #275의 TC 시작·시도별 판정·사람 피드백·사용자 질문 대기를 함께 유지한다. `QaCaseRef`와 첫 QA context에 `verification_procedure`와 `reviews`가 모두 들어간다.
- #262의 `report_step`/`performed` 규약은 이후 승인된 #275의 TC 단위 보고로 대체한다. 참고 step을 다시 필수 판정으로 만들지 않는다. 기대값과 관측값이 모순된 통과를 거절하는 회귀는 `report_test_case`의 checks 계약으로 옮겼다.
- 원본 갈래들이 미병합 `v25`·`v26` 번호를 서로 다른 내용으로 사용했다. 공개 `develop`의 `v1`~`v24`는 원문을 유지한다. 실행판 `v25`·`v26`은 #275의 TC 계약 원문, `v27`~`v29`는 #266까지 누적된 진단 갈래 원문으로 고정했다. 서로 다른 미병합 `v25`·`v26`의 양쪽 원문을 [`pr-consolidation-sources/pr-266`](pr-consolidation-sources/pr-266/v25/system.md)과 [`pr-consolidation-sources/pr-275`](pr-consolidation-sources/pr-275/v25/system.md)에 별도로 보존했다. source SHA와 파일별 원본 byte SHA256은 [`pr-consolidation-prompt-sources.json`](pr-consolidation-prompt-sources.json)에 있다.
- 과거 prompt를 동일 조건으로 재현할 때는 manifest가 가리키는 source commit의 tool schema·arch도 함께 사용해야 한다. 번호가 겹친 진단 갈래의 `v25`·`v26`은 archive와 source branch에서 찾을 수 있다. 현재 기본 실행은 새 TC tool 계약과 `v30`이다.
- 새 내용은 기본 판본 `qa_run/v30`에만 합쳤다. TC 계약에 예외·성능·시각·macro text 점검을 더한다. QA label은 `v34-case-evaluation-diagnostics-procedures`다. 예외·성능 tool이 합쳐진 tool 목록과 fingerprint를 함께 고정했다. 과거 판본의 의미와 default TC 계약을 섞지 않는다.
- 독립 review가 통합 과정의 `v27`~`v29` 덮어쓰기를 발견해 원문으로 복원했다. `tests/test_prompt_source_preservation.py`는 `v1`~`v29` 전체 파일 목록과 SHA256, 미병합 번호 충돌 archive 네 개를 검증한다. 원본 branch와 commit도 그대로 남아 있다.

## 통합 검증

- 통합 전 첫 전체 실행에서 12개 오래된 prompt/tool 단정이 실패했다. 참고 step 보고가 제거된 TC 계약과 결합된 진단 tool에 맞춰 회귀를 수정했다.
- 수정 후 집중 검증: `tests/test_qa_arch.py tests/test_qa_tools.py tests/test_qa_verdict_performed.py tests/test_llm_default_model.py tests/test_qa_bundle_contract.py` 127개 통과.
- 통합 초기 전체 `.venv/bin/python -m pytest -q`: **2,982개 통과**, 201.54초.
- `/home/yunseong/.local/bin/uv build --wheel --out-dir /home/yunseong/dev/artel/.work/agent-pr-bundle-dist-20261028`: wheel build 통과.
- `python -m compileall -q app` 통과. 원문 복원 전 `git diff --check`는 통과했다. 복원한 과거 판본의 기존 EOF 빈 줄 9개는 원본 hash를 보존하기 위해 수정하지 않는다. `git -c core.whitespace=-blank-at-eof diff --check`로 나머지 whitespace 오류가 없음을 확인한다.
- 과거 prompt 보존 수정 후 영향 회귀 **44개 통과**. 최종 전체 `.venv/bin/python -m pytest -q`는 **2,984개 통과**, 201.67초다. `v1`~`v29` source 원문과 충돌 archive hash 회귀 두 개가 추가됐다.
- 실제 LLM·유료 QA·Unity 실행은 시작하지 않았다. 원본의 성능 측정과 실제 QA 검증은 아래 원본 PR 기록이며 이번 통합의 정확도 개선 증거로 사용하지 않는다.

## 다른 작성자의 포함된 선행 PR

원본 11개 source head가 최종 통합 HEAD의 ancestor임을 전수 확인했다. 아래 다른 작성자의 PR도 source가 이미 의존하던 commit으로 포함된다. 해당 PR의 branch·head·상태는 변경하지 않는다.

- [#269 — 판정마다 이유 싣기와 근거 축 verdict_evidence 추가](https://github.com/project-artel/artel-agent-server/pull/269)
- [#270 — 같은 TC의 사람 확정 기록을 QA 첫 메시지에 추가](https://github.com/project-artel/artel-agent-server/pull/270)

## 원본 설명과 검증 이력

다음은 고정한 원본 PR의 설명이다. 명령·수치·미실행 항목과 evidence 링크는 원본 작성 시점의 기록이며 현재 통합 검증과 구분한다.

<details><summary>원본 PR #181: feat(agent): 기본 model 을 DEFAULT_MODEL 환경변수로 정한다</summary>

## Why

이 서버의 기본 model 은 `app/llm/models.py` 의 모듈 상수 하나가 정했고, 값이 `openai/gpt-5.6-luna` 라는 OpenRouter slug 였다. `Settings` 에 대응하는 항목이 없어 `.env` 로는 못 정했고, 바꾸려면 이미지를 다시 빌드해야 했다.

그래서 model 을 안 지정한 호출은 전부 OpenRouter 로 나간다. `POST /extract` 가 그 경로다 — orchestration 의 `artel.agent.extract.model` 이 기본으로 비어 있어 `AgentExtractClient` 가 `model` 필드를 빼고 보내고, 요청 schema 의 기본값이 그대로 쓰인다.

로컬에서 실제로 죽었다. OpenRouter 잔액이 0 이라 (`GET /credits` 가 `total_credits` 200, `total_usage` 200.15) 기획 문서 지식 추출이 실패하고 문서가 `parse_status=FAILED` 로 남았다. 같은 stack 의 QA 런은 요청에 Bedrock slug 를 실어 보내므로 멀쩡히 돌았다.

`qa_compaction_model` 이 ARTEL-776 에서 겪은 것과 같은 실패다. 그때는 압축이 런의 model 을 따라가게 고쳤고, 남아 있던 마지막 자리가 기본값 자체다.

## What Changed

`Settings.default_model` 을 더하고 `app/llm/default_model.py` 가 그것을 읽는다. 비우면 `FALLBACK_MODEL` 을 따르므로 **설정하지 않은 배포는 종전과 완전히 같다.** catalog 밖 값은 첫 `get_settings()` 에서 거절된다.

| 준 값 | 요청 기본값 | 지어진 client |
|---|---|---|
| `DEFAULT_MODEL=bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0` | 같은 값 | `ChatBedrockConverse`, `us-west-2` |
| 안 줌 | `openai/gpt-5.6-luna` | `ChatOpenAI` |
| `DEFAULT_MODEL=anthropic/nope` | — | `get_settings()` 가 거절 |

기존 요청 계약은 그대로다. `model` 을 지정해 보내는 호출자는 영향이 없다.

**commit 두 개가 서로 상쇄한다** (`9b492ac`, `a9b50f4`). Bedrock Claude 3.7 Sonnet · 3.5 Sonnet · 3 Opus 를 catalog 에 넣었다가 되돌린 것이다. Bedrock control plane 에 물어보니 셋 다 이 계정 `us-west-2` 에 없다 — foundation model 목록에도, `ACTIVE` inference profile 73 개 중에도 없고, Claude 3 세대에서 남은 것은 `claude-3-haiku-20240307-v1:0` 하나다. catalog 에 넣으면 `/internal/models` 에는 뜨지만 고르는 순간 거절된다. 그 변경의 유일한 기록이라 지우지 않고 남겼다. net diff 에는 안 들어간다.

## Example

```bash
# artel-agent-server/.env
DEFAULT_MODEL="bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
```

```
POST /internal/extract  {"source_url": "...", "filename": "spec.pdf"}
→ model 을 안 실어 보내도 Bedrock 으로 나간다
```

## Code Walkthrough

- `app/config.py:47` — `default_model: str | None`. `LLMModel` 로 타입을 못 붙이는 이유는 `qa_compaction_model` 과 같다 — `app.llm.__init__` 이 `chat_model` 을 끌어오고 그것이 이 module 을 다시 부른다.
- `app/config.py:141` — `known_model` validator 가 `qa_compaction_model` 과 `default_model` 둘을 받는다. 같은 질문("catalog 에 있나")이라 validator 를 하나 더 만들지 않았고, 오류 문구의 필드 이름은 `ValidationInfo.field_name` 에서 온다. `qa_compaction_model` 의 문구는 안 바뀐다.
- `app/llm/default_model.py:20,23` (신규) — `FALLBACK_MODEL` 과 `resolve_default_model()`. `models.py` 에 안 둔 것은 그 파일이 바깥 import 가 하나도 없는 catalog 라서다. 어느 model 이 있는지는 값이고, 그중 무엇이 기본인지는 이 배포의 설정이다. 함수 docstring 이 이 변경의 근거를 한 곳에 든다 — 호출부 16 곳에는 주석을 안 달았다.
- `app/llm/models.py` — `DEFAULT_MODEL` 상수를 뺐다. 이름을 `FALLBACK_MODEL` 로 바꾼 것은 환경변수와 설정 항목이 `DEFAULT_MODEL` 이라, 셋이 같은 이름이면 "상수는 기본이 아니라 못 정했을 때의 값" 을 읽는 사람이 외워야 하기 때문이다.
- Pydantic field 11 곳 (`app/api/extract.py:20`, `qa_sessions.py`, `sessions.py`, `knowledge_queries.py`, `step_phrasing.py`, `app/sessions/schemas.py`, `app/agents/*/schemas.py`) — `Field(default_factory=resolve_default_model)`. Pydantic 은 class 가 정의될 때 default 를 한 번 읽으므로 상수를 적어 두면 설정을 안 따라간다.
- 함수 인자 5 곳 (`app/documents/service.py:42`, `app/sessions/service.py`, `app/qa/run_config.py:110`, `app/qa/screen_verdict.py`, `app/agents/knowledge_query/agent.py`) — `LLMModel | None = None` 을 받고 body 첫 줄에서 채운다. `LLMModel` 에 falsy 값이 없어 `model or resolve_default_model()` 이 안전하다.
- `app/qa/service.py:78` — signature 만 바꿨다. 이 method 에서 `model` 의 유일한 사용처가 `resolve_run_config(model=model, ...)` 이고 그 함수가 같은 해석을 하므로, body 에서 또 하는 것은 죽은 줄이다. pair review 지적.
- `tests/conftest.py` (신규) — `app` import 전에 `os.environ["DEFAULT_MODEL"] = ""`. `os.environ` 이 `_env_file` 을 이기고 `tests/test_qa_run_config_contract.py:34` 는 module 수준에서 `TestClient(app)` 을 만들어, `DEFAULT_MODEL` 을 적어 둔 기계에서는 model 과 provider 와 reasoning 예산 단정이 함께 깨진다. fixture 로는 그 시점을 못 잡는다.
- `tests/test_llm_default_model.py` (신규) — 7 개. `test_every_request_schema_reads_the_setting` 이 11 개 schema 의 `default_factory` 를 pin 하고, `test_a_stored_session_keeps_the_model_it_was_opened_with` 이 저장된 session 이 지금 설정을 안 따라가는 것을 지킨다.
- `.env.example:11` — 항목과 설명.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest` → **926 passed, 2 failed**
- [x] 기준선(변경 전) → `917 passed, 2 failed`. 깨진 둘이 같다: `test_settings_can_load_from_env_file` 과 `test_the_new_name_wins_over_the_old_one`. 이 기계의 `~/.zshenv:10` 이 `OPENROUTER_API_KEY` 를 export 하고 `os.environ` 이 임시 `.env` 를 이겨서 `llm_api_key` 단정에서만 난다. 이 변경과 무관하고 고치지 않았다.
- [x] 세 경로를 직접 확인 — 위 What Changed 의 표.
- [x] mutation 확인: `InMemorySessionStore.save` 에 `exclude_defaults=True` 를 붙이고 `ExtractRequest.model` 을 상수 default 로 되돌리니 테스트 셋이 깨졌고, 되돌리니 통과했다. 새 테스트가 tautology 가 아니라는 근거다.
- [x] OpenAPI: `ExtractRequest` 의 `required` 는 `['source_url', 'filename']` 로 `model` 이 여전히 optional 이다.
- [ ] 로컬 stack 을 띄운 실제 `POST /extract` — 안 돌렸다. 위의 client 확인이 같은 것을 코드 수준에서 본다.

## Risks

- `Field(default_factory=...)` 는 OpenAPI schema 에서 `default` key 를 지운다. field 는 optional 로 남으므로(확인) 클라이언트가 깨지지는 않고, 문서에 기본값이 안 보이는 것만 바뀐다. 11 개 요청 schema 전부에 해당한다.
- `app/llm/default_model.py` 가 `app/config.py` 를 import 하므로, 나중에 누가 `config.py` 맨 위에 `app.llm` import 를 넣으면 순환이 생긴다. 그것을 금지하는 주석이 두 곳에 있다.
- 잘못된 `DEFAULT_MODEL` 을 준 배포는 startup 에서 죽는다. 의도한 것이다 — 조용히 다른 model 로 도는 것보다 낫고, `qa_compaction_model` 이 이미 같은 규칙이다.

## Rollback

`git revert`. 설정을 안 준 배포에는 동작 변화가 없으므로 되돌려도 잃는 것이 없다. 급하면 `.env` 에서 `DEFAULT_MODEL` 을 비우는 것만으로 종전 동작이다.

계획: `.plan/general/2026-09-10-default-model-from-env.md`

Jira: ARTEL-884

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Khqf8ZXSp4wq14vdLyMxme


</details>

<details><summary>원본 PR #257: fix: planner 설명과 검증 결과 출력 언어 적용</summary>

## Why

planner_executer가 run_config.language를 기록만 하고 네 모델 prompt에 전달하지 않아,
WordVenture CI에서 ko 설정으로 실행해도 영어 설명과 검증 결과가 나왔습니다.

## What Changed

- ORDERED·FREE planner/evaluator에 출력 언어 지시문을 전달합니다.
- 진행 설명, 완료 결과, 미도달·판정 오류 설명을 ko/en으로 선택합니다.
- 코드 식별자, game text, 비교 literal과 원본 parser 오류는 보존합니다.
- 언어 지시문 hash를 기록하고 구조 label을 pe3-output-language로 변경합니다.

## Example

테스트 fixture에서 language=ko인 실행의 네 prompt 모두 한국어 출력 지시문을 받습니다.
FREE run의 최종 STATUS는 `전체 1개 step 중 0개를 통과했습니다.`입니다.
en은 기존 영어 문장을 사용합니다. 이 fixture는 전달과 출력 형태를 검증하며 실제 Sonnet 응답을 증명하지 않습니다.

## Code Walkthrough

- `app/agents/planner_executer/prompts.py` — 언어 지시문과 hash를 구성합니다.
- `app/agents/planner_executer/run_config.py` — 선택한 언어를 hash 계산에 전달합니다.
- `app/agents/planner_executer/planner.py` — ORDERED/FREE prompt와 진행 설명에 언어를 적용합니다.
- `app/agents/planner_executer/evaluator.py` — 두 evaluator와 오류 결과에 언어를 적용합니다.
- `app/agents/planner_executer/runner.py` — 진행·완료·미도달 결과 문구를 선택합니다.
- `app/agents/planner_executer/arch.py` — 변경된 모델 입력을 pe3-output-language로 기록합니다.
- `tests/test_planner_executer_language.py` — 네 prompt와 실제 channel ko/en 출력, hash를 검사합니다.

## Validation

- `uv run --extra dev python -m pytest -q`: 2848 passed, 1 failed.
- 실패는 변경하지 않은 `test_qa_macro_model.py:320`의 Windows 경로 비교입니다. `agents\\qa\\macro\\parser.py`를 `agents/qa/macro/parser.py`와 비교합니다.
- planner 관련 94개 테스트는 통과했습니다. pair-review PASS, git diff --check 통과.
- 변경 전 stage run 136: Sonnet, prompt v3, pe2-free, fingerprint 236e9f8398d9로 1회 실행. 개인 key 호출 46회 후 최종 TC 판정 없이 FAILED.
- 변경 후 실제 게임 비교 실행은 아직 수행하지 않았습니다. TC 통과율이나 agent 성능 개선을 주장하지 않습니다.

## Risks

stage 배포 후 개인 Sonnet과 planner_executer로 한국어 결과를 실제 확인해야 합니다.
화면 반전 및 기존 CI 중단 문제는 이 변경으로 해결하지 않습니다. qa-action release는 아직 보류합니다.
Jira connector 재인증 오류로 이슈 등록을 완료하지 못했습니다.

## Rollback

이 commit을 revert합니다. API와 데이터 schema는 변경하지 않습니다.


</details>

<details><summary>원본 PR #261: fix(qa): QaArchSpec 의 vision 에 boolean 수용</summary>

## Why

run_config 에 저장되는 `arch.vision` 은 resolve 된 boolean 이다 (`"vision": false`). 그런데 요청의 `QaArchSpec.vision` 은 `auto`/`on`/`off` 만 받아서, 저장된 run_config 를 그대로 다시 보내면 422 가 났다. orchestration 은 그 422 를 503 `upstream_unavailable` 로 바꿔 사용자는 agent-server 가 죽은 것으로 읽었다 (2026-10-22, `.work/eval-feedback-20261022/F-gyemong/results.md` 문제 2).

이 PR 은 ARTEL-1236 의 agent-server 쪽이다. orchestration 이 4xx 본문을 그대로 넘기는 쪽은 별도 PR 이다.

## What Changed

- `QaArchSpec.vision` 이 `true` 를 `on`, `false` 를 `off` 로 받는다. 문자열 세 값은 종전 그대로다.
- `auto` 로 받지 않는다. 저장된 `true` 는 "보여 준다" 를 고른 결과지 "모델에 맡긴다" 가 아니다.
- 422 문장을 고쳤다. orchestration 이 agent-server 의 4xx 를 `agent_rejected_request` 의 `message`·`fields` (`detail[].loc/msg`)로 그대로 넘기게 되어 (orchestration #367~#371) 이 문장이 사용자에게 간다.
  - `arch: null` → `loc ["body","arch"]`, `Value error, arch must be an object such as {"vision": "on"}; leave the field out for the default structure, null is not accepted`
  - `arch: {"vision": "sometimes"}` → `loc ["body","arch","vision"]`, `Value error, vision must be 'auto', 'on' or 'off' (true and false from a stored run_config are read as 'on' and 'off'), not 'sometimes'`
  - `null` 을 기본 구조로 받지 않는 것은 "구조를 모른다" 와 "기본 구조" 가 한 값이 되기 때문이다. 이슈의 확인 방법도 `arch: null` 이면 필드 이름이 든 422 를 기대한다.

## Example

`tests/test_qa_run_config_contract.py` 의 fixture 로 확인한 왕복이다 (stub store, 실제 orchestration 없음).

1. `POST /internal/qa-sessions` 에 `arch: {"vision": "off"}` → 응답 `run_config.arch.vision = false`
2. 그 값을 그대로 `arch: {"vision": false}` 로 다시 보냄 → 200, `run_config.arch.vision = false`, `agent_fingerprint` 가 1번과 같다
3. `arch: {"vision": true}` → 200, `run_config.arch.vision = true`

이 예시는 agent-server 의 계약만 보인다. orchestration 을 거친 재실행은 확인하지 않았다.

## Code Walkthrough

- `app/api/qa_sessions.py` `OpenQaSessionRequest.arch_is_an_object` — `arch: null` 을 위 문장으로 거절한다.
- `app/agents/qa/arch.py` `QaArchSpec.vision_accepts_a_boolean` — `mode="before"` field validator. 값이 `VisionMode` 로 바뀌므로 `model_dump()` 와 `arch_fingerprint` 는 문자열로 받은 것과 같다. `QA_ARCH_LABEL` 과 핀된 fingerprint 는 움직이지 않는다.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 2887 passed
- [x] 새 테스트: `tests/test_qa_arch.py::test_a_stored_boolean_vision_is_read_as_on_or_off`, `tests/test_qa_run_config_contract.py::test_a_stored_run_config_arch_replays_as_it_was_stored`, `test_a_null_or_unknown_arch_value_is_refused_with_a_readable_message`
- [ ] orchestration 을 거친 재실행 — 하지 않음

## Risks

- `vision: 1` 같은 정수는 종전처럼 거절된다. `True`/`False` 객체만 바꾼다.

## Rollback

이 커밋을 revert 한다. 저장 형식과 fingerprint 가 그대로라 데이터 정리는 필요 없다.

Jira: ARTEL-1236

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #262: fix(qa): report_step 판정을 기대값과 관찰값 대조로 변경</summary>

## Why

2026-10-22 L1 측정 (prompt v24, Claude Sonnet 5.5)에서 거짓 기대값 5개 중 4개 (step 5, 8, 11, 14)가 매번 통과로 보고됐다. message 는 "발동 버튼이 표시/활성화되지 않아 눌릴 수 없음" 처럼 거부를 맞게 적고 `passed=true` 였다. 첫 메시지와 `tool_report_step.md` 가 검증 step 이 아닌 step 의 `passed` 를 "행동을 했는가" 로 정의한 것이 그 해석을 열어 두었다.

## What Changed

- `report_step` 과 `report_case` (네 모양 모두)에 `performed: bool` 을 더하고, 인자 순서를 `message`, `performed`, `passed` 로 바꿨다. 모델은 schema 순서대로 채우므로 `expected:`/`observed:` 두 줄이 판정보다 먼저 적힌다.
- `performed=false` 이면서 `passed=true` 인 보고는 step 마다 한 번 돌려보낸다. 같은 보고를 다시 보내면 기록한다 — 기대값 자체가 "누를 수 없다" 인 step 에서는 그 조합이 맞다.
- 첫 메시지 (`runner._plan`, `_free_plan`)의 판정 규칙을 같은 정의로 고쳤다.
- prompt `qa_run/v25`: `## How to work` 4번에 두 줄 형식과 거부된 행동을 실패로 적는 예시, 판정 tool 설명 넷.
- `QA_ARCH_LABEL` → `v28-verdict-expected-observed`, fingerprint `7d56243753d1` → `2fd1bb6585a3`.

## Example

`tests/test_qa_verdict_performed.py` 의 fixture (실제 게임 없음):

1. `report_step({"step": 1, "message": "expected: 발동 버튼이 나타난다\nobserved: 버튼이 숨겨져 있어 누를 수 없음", "performed": false, "passed": true, ...})` → "Nothing was recorded — you reported `passed=true` for an action you did not carry out …", `STATUS` frame 없음, `step_results` 비어 있음
2. 같은 호출을 다시 → "Recorded.", `STATUS` `COMPLETED` 하나
3. `performed` 는 `qa_log` 의 `TOOL` 행 `payload.args.performed` 에 남는다. `STATUS` frame 에는 싣지 않는다 (`learned` 와 같은 자리).

## Code Walkthrough

- `app/agents/qa/tools/reporting_tools.py:199` `_unperformed_pass_refusal` — 한 번 돌려보내고 phase 를 VERIFY 에 잡아 둔다. `_record_verdict` 맨 앞에서 부른다.
- `app/agents/qa/tools/reporting_tools.py:445` 이하 — 네 판정 tool 의 서명. 인자 순서가 바뀐 이유를 주석에 적었다.
- `app/agents/qa/runner.py` `_plan` — 첫 메시지 규칙 세 줄.
- `app/agents/qa/tools/state.py` `unperformed_pass_refused` — 돌려보낸 step.
- `app/prompts/qa_run/v25/` — v24 복사본에서 다섯 role 만 고쳤다. `prompts-lock.json` 재생성.
- `app/agents/qa/arch.py` — label 과 그 이유 문단.
- 테스트: 기존 report_step 호출 76곳에 `"performed": True` 를 더했다 (스크립트, 의미 변경 없음).

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 이 branch 2893 passed, 스택 맨 위 2927 passed
- [x] 새 테스트 `tests/test_qa_verdict_performed.py` 6개, `test_message_and_performed_come_before_passed_in_the_schema`, `test_v25_*` 2개
- [x] 실제 런 1회, 아래

### 실제 런 (스택 맨 위 `2200d76` 로 1회)

이 스택 다섯 PR(ARTEL-1220, 1219, 1221, 1222, 1223)을 모두 얹은 branch 로 한 번 돌렸다. 각 변경의 몫을 따로 가르지 못한다.

- 시나리오: WordVenture L1 (test run 4, 24 step), CLEAN 빌드 `WordVentureEvalClean1`, eval stack `artel_eval_20261022`
- 축: model `claude/claude-sonnet-5-5`, `contentMapMode=frozen`, `knowledgeMode=off`, `screen_capture=on_demand`, 게임 하나씩
- 채점: `.agents/skills/qa-run-local/SKILL.md` 의 dedupe SQL (`B-seeded-defects/sql/score.sql`), 거짓 기대값은 step 5, 8, 11, 12, 14

| qa_try | `agent_arch` | `prompt_version` | `agent_fingerprint` | 판정 | 일치 /24 | 거짓 기대값 잡음 /5 | 오탐 | 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 246 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 19 | 1 (12) | 1 (19) | $4.89 |
| 249 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 20 | 1 (12) | 0 | $4.42 |
| 252 (후) | `v32-performance` | `v29` | `04952982dbf6` | 24 | 16 | **3 (5, 11, 12)** | 6 (18, 19, 21–24) | $6.15 |

읽는 법:

- **1회다.** 결과가 아니라 방향이다. `prompt_version` 도 함께 움직였다 — prompt 가 이 변경의 일부라 같게 둘 수 없었다.
- 거짓 기대값은 1 → 3. step 5 와 11 은 `performed=true`, `passed=false` 로 왔다 ("expected: 오른쪽 방향키로 다음 노드로 이동 / observed: … Stage 0 유지"). step 8 과 14 는 여전히 통과였다. 둘 다 agent 가 `expected:` 줄에 기대값이 아니라 행동을 적었다 ("expected: spell 카드만 슬롯에 있는 상태에서 발동 버튼이 눌릴 수 있는지 확인"). 이슈 목표(4 이상)에는 못 미친다.
- 오탐 6개는 step 18 이후다. 이 런에서 action tool 답 34개가 "The game reported no result" 였고 (전 두 런은 0), orchestration log 에서 `ACTION_RESULT` 가 30~60초 늦게 도착한 것이 보인다. agent 는 step 21~24 를 "입력이 수십 초 지연되어 진행 불가", `SETUP-FAILED` 로 적었다. 게임 쪽 지연이고 agent-server 가 결과 도착 시각을 바꿀 길은 없다고 보지만, 이 런 하나로는 변경과 떼어 증명하지 못한다.
- ARTEL-1221: step 1, 4, 7 에서 `capture_screen` 을 "첫 캡처", "맵 씬 첫 캡처", "전투 씬 첫 캡처" 로 불렀다. 전 두 런은 step 1 에서 찍지 않았다. CLEAN 빌드라 잡을 시각 결함은 없었다.
- ARTEL-1222: `advance_story` 답에 37줄이 실렸다. `AdvancePrompt` 의 "Click / Any key - Next/Show all" 이 대사 사이마다 끼어 줄 수의 절반가량을 차지한다.
- ARTEL-1219: 이 SDK 는 아직 `errors` 를 안 보내므로 `unseen errors` 줄이 나올 일이 없었다.
- ARTEL-1223: agent 가 step 15 에서 `get_performance` 를 한 번 불렀고 "orchestration answered 404" 를 받았다. eval orchestration 에 endpoint 가 아직 없다.
- `MAX_LOGGED_CHARS` 가 tool 결과를 4,000자에서 자르므로 tool 결과 끝에 붙는 줄이 `qa_log` 에 없는 것은 근거가 되지 않는다.

## Risks

- agent 가 `expected:` 에 기대값 대신 행동을 적으면 이 규칙은 걸리지 않는다 (위 런의 step 8, 14). 다음 손은 첫 메시지의 `verify` 를 `expected:` 에 그대로 옮기게 하거나 서버가 `verify` 원문을 답에 대 주는 것이다.
- `QA_PROMPT_VERSION` 을 v24 이하로 고정한 배포는 `performed` 를 모르는 설명으로 새 schema 를 받는다.

## Rollback

이 커밋을 revert 한다. 저장 형식은 바뀌지 않았다 (`performed` 는 `qa_log` 의 tool 인자에만 있다).

Jira: ARTEL-1220

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #263: feat(qa): tool 결과에 안 본 예외 수 한 줄과 get_errors tool 추가</summary>

## Why

SDK 가 `PULSE` 와 `ACTION_RESULT` 에 `errors` 를 싣는다 (ARTEL-1218, artel-sdk PR #154). 전부를 매 호출 context 에 넣으면 context 가 불고 오탐이 는다 — gyemong 은 scan 만으로 게임 자체 예외 2종이 나고, luna 계열은 L1 170런 중 169런에 오탐이 있었다 (`.work/eval-feedback-20261022/report.md` 6절, 7절).

## What Changed

- `QaRunChannel.game_errors` (`app/qa/game_errors.py`)가 `(type, message, stackHead)` 로 항목을 모은다.
- `ToolContext.answer` 를 지나는 tool 결과 끝에 안 본 항목이 있으면 `unseen errors: N` 한 줄. 0 이면 줄이 없다.
- 새 tool `get_errors(include_seen=false)` 가 안 본 항목을 보여 주고 본 것으로 표시한다. 본 항목은 `count` 가 늘어도 다시 세지 않는다. 기준선은 없다. `ANY_PHASE` 라 ACT 뒤 VERIFY 전에 불러도 거절되지 않는다.
- SDK 계약 (PR #154)에 맞췄다: `truncated` 는 항목의 `message` 가 200자에서 잘렸다는 뜻이고, 20개를 넘는 항목은 다음 `pulse` 로 넘어간다. SDK 자신의 `[Artel]` Error log 는 `N` 에 넣지 않고 `get_errors` 가 따로 묶는다.
- 예외는 step 판정이 아니라 `report_issue` 로 간다는 문장을 tool 답과 prompt 에 둔다.
- prompt `qa_run/v26`: system prompt 에 문단 하나, `tool_get_errors.md`.
- `QA_ARCH_LABEL` → `v29-unseen-errors`, fingerprint → `30d40310f4fd`.
- `get_errors` 호출과 결과는 다른 tool 처럼 runner 의 `_log_tool_call` 이 `qa_log` 에 `TOOL`/`TOOL_RESULT` 로 남긴다.

## Example

`tests/test_qa_game_errors.py` 의 fixture (SDK 계약 모양, 실제 게임 없음):

1. `PULSE` payload `{"errors": [{"type": "NullReferenceException", "message": "story advance handler", "stackHead": "StoryManager.Advance () …", "firstFrame": 120, "lastFrame": 121, "count": 1}]}`
2. `observe_scene` 답의 끝: `unseen errors: 1`
3. `get_errors` → `1 unseen error(s), now marked seen:\n- NullReferenceException: story advance handler (×1, first frame 120, last frame 121)\n    at StoryManager.Advance () …`
4. 같은 항목이 `count: 9` 로 다시 와도 다음 답에 줄이 없다.

## Code Walkthrough

- `app/qa/game_errors.py` — 장부, wire 파싱, `get_errors` 답.
- `app/qa/channel.py` `on_pulse`, `on_action_result` — 두 frame 에서 `errors` 를 받는다. 기다리는 쪽이 없는 늦은 `ACTION_RESULT` 도 받는다.
- `app/agents/qa/tools/tool_context.py` `answer` — 한 줄을 붙이는 자리. 화면을 안 붙이는 tool 에도 붙인다.
- `app/agents/qa/tools/observation_tools.py` `get_errors`.
- `app/agents/qa/tools/phase.py` — `_TOOL_PHASE` 의 OBSERVE, `ANY_PHASE`. `ALWAYS_ALLOWED` 에 안 넣은 이유는 locked `phase_directive` 가 그 목록을 전부 적어야 해서다.
- `app/prompts/qa_run/v26/`, `app/agents/qa/arch.py`.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 스택 맨 위 2927 passed
- [x] 새 테스트 `tests/test_qa_game_errors.py` 12개, `test_v26_*`
- [ ] 예외를 심은 빌드의 3런 — 하지 않음. 이 eval 의 SDK 가 아직 `errors` 를 안 보낸다.

### 실제 런 (스택 맨 위 `2200d76` 로 1회)

이 스택 다섯 PR(ARTEL-1220, 1219, 1221, 1222, 1223)을 모두 얹은 branch 로 한 번 돌렸다. 각 변경의 몫을 따로 가르지 못한다.

- 시나리오: WordVenture L1 (test run 4, 24 step), CLEAN 빌드 `WordVentureEvalClean1`, eval stack `artel_eval_20261022`
- 축: model `claude/claude-sonnet-5-5`, `contentMapMode=frozen`, `knowledgeMode=off`, `screen_capture=on_demand`, 게임 하나씩
- 채점: `.agents/skills/qa-run-local/SKILL.md` 의 dedupe SQL (`B-seeded-defects/sql/score.sql`), 거짓 기대값은 step 5, 8, 11, 12, 14

| qa_try | `agent_arch` | `prompt_version` | `agent_fingerprint` | 판정 | 일치 /24 | 거짓 기대값 잡음 /5 | 오탐 | 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 246 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 19 | 1 (12) | 1 (19) | $4.89 |
| 249 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 20 | 1 (12) | 0 | $4.42 |
| 252 (후) | `v32-performance` | `v29` | `04952982dbf6` | 24 | 16 | **3 (5, 11, 12)** | 6 (18, 19, 21–24) | $6.15 |

읽는 법:

- **1회다.** 결과가 아니라 방향이다. `prompt_version` 도 함께 움직였다 — prompt 가 이 변경의 일부라 같게 둘 수 없었다.
- 거짓 기대값은 1 → 3. step 5 와 11 은 `performed=true`, `passed=false` 로 왔다 ("expected: 오른쪽 방향키로 다음 노드로 이동 / observed: … Stage 0 유지"). step 8 과 14 는 여전히 통과였다. 둘 다 agent 가 `expected:` 줄에 기대값이 아니라 행동을 적었다 ("expected: spell 카드만 슬롯에 있는 상태에서 발동 버튼이 눌릴 수 있는지 확인"). 이슈 목표(4 이상)에는 못 미친다.
- 오탐 6개는 step 18 이후다. 이 런에서 action tool 답 34개가 "The game reported no result" 였고 (전 두 런은 0), orchestration log 에서 `ACTION_RESULT` 가 30~60초 늦게 도착한 것이 보인다. agent 는 step 21~24 를 "입력이 수십 초 지연되어 진행 불가", `SETUP-FAILED` 로 적었다. 게임 쪽 지연이고 agent-server 가 결과 도착 시각을 바꿀 길은 없다고 보지만, 이 런 하나로는 변경과 떼어 증명하지 못한다.
- ARTEL-1221: step 1, 4, 7 에서 `capture_screen` 을 "첫 캡처", "맵 씬 첫 캡처", "전투 씬 첫 캡처" 로 불렀다. 전 두 런은 step 1 에서 찍지 않았다. CLEAN 빌드라 잡을 시각 결함은 없었다.
- ARTEL-1222: `advance_story` 답에 37줄이 실렸다. `AdvancePrompt` 의 "Click / Any key - Next/Show all" 이 대사 사이마다 끼어 줄 수의 절반가량을 차지한다.
- ARTEL-1219: 이 SDK 는 아직 `errors` 를 안 보내므로 `unseen errors` 줄이 나올 일이 없었다.
- ARTEL-1223: agent 가 step 15 에서 `get_performance` 를 한 번 불렀고 "orchestration answered 404" 를 받았다. eval orchestration 에 endpoint 가 아직 없다.
- `MAX_LOGGED_CHARS` 가 tool 결과를 4,000자에서 자르므로 tool 결과 끝에 붙는 줄이 `qa_log` 에 없는 것은 근거가 되지 않는다.

## Risks

- SDK 쪽이 머지되기 전에는 이 기능이 동작할 데이터가 없다. 계약이 다시 바뀌면 `errors_in` 만 고치면 된다.
- 전달에 실패한 `pulse` 의 항목은 SDK 가 다시 보내지 않으므로 장부는 게임이 낸 예외 전부가 아니다.

## Rollback

이 PR 의 커밋을 revert 한다. 저장 형식은 바뀌지 않았다.

Jira: ARTEL-1219

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #264: feat(qa): screen capture 답에 시각 이상 점검 목록과 scene 첫 진입 capture 요청 추가</summary>

## Why

2026-10-22 측정에서 타이틀 배경이 없는 빌드 (`VIS-1`) 0/3, 카드 그림이 없는 빌드 (`VIS-3`) 0/3 이었다. 앞의 것은 step 1 에서 screen capture 를 찍은 런이 없었고, 뒤의 것은 3런 모두 손패를 capture 하고도 그림이 없다는 말이 없었다.

## What Changed

- `capture_screen` 답 끝에 점검 목록 (`CAPTURE_CHECKLIST`): 목적에 답한 뒤 빈 영역, 그림 없는 요소, 겹침, 잘림, 개발자 콘솔을 한 번 더 보고 찾은 것은 `report_issue` 로.
- vision 이 켜진 `on_demand` 런에서 scene 의 view 가 처음 그려진 tool 결과에 "First view of scene `X` this run: call `capture_screen` once …" 한 줄을 scene 마다 한 번.
- prompt `qa_run/v27`: `vision_directive` 에 같은 두 규칙.
- `QA_ARCH_LABEL` → `v30-capture-checklist`. tool 과 schema 는 그대로라 fingerprint 는 안 움직인다.

## Example

`tests/test_qa_capture_checklist.py` (fixture): `TitleScene` 의 whole `pulse` 뒤 첫 `observe_scene` 답에 `First view of scene `TitleScene` this run` 줄이 있고, 두 번째에는 없으며, `StoryScene` 으로 바뀐 뒤 다시 한 번 붙는다. 아래 실제 런에서 agent 는 step 1, 4, 7 에 "첫 캡처" 로 `capture_screen` 을 불렀다.

## Code Walkthrough

- `app/agents/qa/tools/tool_context.py` `_first_entry_line` — scene 마다 한 번, `every_call`·`by_role`·vision 꺼짐에는 없다.
- `app/agents/qa/tools/observation_tools.py` `CAPTURE_CHECKLIST` — 그림이 도착하기 직전에 읽히는 자리라 system prompt 대신 여기 둔다. system prompt 는 8,000자 상한에 76자 남았다.
- `app/agents/qa/tools/state.py` `scenes_entered`.
- `app/prompts/qa_run/v27/vision_directive.md`, `app/agents/qa/arch.py`.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 스택 맨 위 2927 passed
- [x] 새 테스트 `tests/test_qa_capture_checklist.py` 3개, `test_v27_extends_only_the_vision_directive`
- [ ] `VIS-1`, `VIS-3` 빌드 각 3런 — 하지 않음 (예산)

### 실제 런 (스택 맨 위 `2200d76` 로 1회)

이 스택 다섯 PR(ARTEL-1220, 1219, 1221, 1222, 1223)을 모두 얹은 branch 로 한 번 돌렸다. 각 변경의 몫을 따로 가르지 못한다.

- 시나리오: WordVenture L1 (test run 4, 24 step), CLEAN 빌드 `WordVentureEvalClean1`, eval stack `artel_eval_20261022`
- 축: model `claude/claude-sonnet-5-5`, `contentMapMode=frozen`, `knowledgeMode=off`, `screen_capture=on_demand`, 게임 하나씩
- 채점: `.agents/skills/qa-run-local/SKILL.md` 의 dedupe SQL (`B-seeded-defects/sql/score.sql`), 거짓 기대값은 step 5, 8, 11, 12, 14

| qa_try | `agent_arch` | `prompt_version` | `agent_fingerprint` | 판정 | 일치 /24 | 거짓 기대값 잡음 /5 | 오탐 | 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 246 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 19 | 1 (12) | 1 (19) | $4.89 |
| 249 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 20 | 1 (12) | 0 | $4.42 |
| 252 (후) | `v32-performance` | `v29` | `04952982dbf6` | 24 | 16 | **3 (5, 11, 12)** | 6 (18, 19, 21–24) | $6.15 |

읽는 법:

- **1회다.** 결과가 아니라 방향이다. `prompt_version` 도 함께 움직였다 — prompt 가 이 변경의 일부라 같게 둘 수 없었다.
- 거짓 기대값은 1 → 3. step 5 와 11 은 `performed=true`, `passed=false` 로 왔다 ("expected: 오른쪽 방향키로 다음 노드로 이동 / observed: … Stage 0 유지"). step 8 과 14 는 여전히 통과였다. 둘 다 agent 가 `expected:` 줄에 기대값이 아니라 행동을 적었다 ("expected: spell 카드만 슬롯에 있는 상태에서 발동 버튼이 눌릴 수 있는지 확인"). 이슈 목표(4 이상)에는 못 미친다.
- 오탐 6개는 step 18 이후다. 이 런에서 action tool 답 34개가 "The game reported no result" 였고 (전 두 런은 0), orchestration log 에서 `ACTION_RESULT` 가 30~60초 늦게 도착한 것이 보인다. agent 는 step 21~24 를 "입력이 수십 초 지연되어 진행 불가", `SETUP-FAILED` 로 적었다. 게임 쪽 지연이고 agent-server 가 결과 도착 시각을 바꿀 길은 없다고 보지만, 이 런 하나로는 변경과 떼어 증명하지 못한다.
- ARTEL-1221: step 1, 4, 7 에서 `capture_screen` 을 "첫 캡처", "맵 씬 첫 캡처", "전투 씬 첫 캡처" 로 불렀다. 전 두 런은 step 1 에서 찍지 않았다. CLEAN 빌드라 잡을 시각 결함은 없었다.
- ARTEL-1222: `advance_story` 답에 37줄이 실렸다. `AdvancePrompt` 의 "Click / Any key - Next/Show all" 이 대사 사이마다 끼어 줄 수의 절반가량을 차지한다.
- ARTEL-1219: 이 SDK 는 아직 `errors` 를 안 보내므로 `unseen errors` 줄이 나올 일이 없었다.
- ARTEL-1223: agent 가 step 15 에서 `get_performance` 를 한 번 불렀고 "orchestration answered 404" 를 받았다. eval orchestration 에 endpoint 가 아직 없다.
- `MAX_LOGGED_CHARS` 가 tool 결과를 4,000자에서 자르므로 tool 결과 끝에 붙는 줄이 `qa_log` 에 없는 것은 근거가 되지 않는다.

## Risks

- scene 마다 capture 가 하나씩 늘어 비용이 조금 는다. 위 런에서 capture 는 5회였다.
- macro 가 지나간 scene (위 런의 `StoryScene`)은 view 가 그려지지 않아 줄이 붙지 않는다.

## Rollback

이 PR 의 커밋을 revert 한다.

Jira: ARTEL-1221

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #265: fix(qa): macro 답에 macro 가 도는 동안 뜬 글자 목록 추가</summary>

## Why

2026-10-22 측정에서 오프닝 한 줄을 `[MISSING TEXT: STORY_003]` 로 바꾼 빌드 (`OBS-2`)를 3런 중 0런이 찾았다. `advance_story` macro 가 Return 을 12~22회 누르는 동안 `PulseMemory` 는 객체마다 마지막 글자만 쥐어서, 지나간 대사는 모델 앞에 한 번도 오지 않았다.

## What Changed

- `QaRunChannel.shown_text` (`app/qa/shown_text.py`)가 `pulse` 의 `text` 를 도착 순서대로 적는다. 타자 효과로 앞부분이 같은 채 길어지는 글자는 같은 줄을 늘린다.
- `run_macro` 와 `resume_macro` 답이 그 macro 가 도는 동안 뜬 줄을 최대 40줄 싣고, 빈 줄, placeholder (`[MISSING`, `TODO`, key 이름), 깨진 글자를 `report_issue` 로 보내라는 문장을 붙인다. `advance_story` 만이 아니라 모든 macro 에 같다.
- 글자 줄이 붙은 결과는 끝까지 갔어도 macro result marker 로 감싸 step 이 보고된 뒤 `fold` 된다.
- prompt `qa_run/v28`: macro skill 수명주기 절에 문단 하나.
- `QA_ARCH_LABEL` → `v31-macro-shown-text`. fingerprint 는 안 움직인다.

## Example

아래 실제 런 (qa_try 252)의 `advance_story` 답 앞부분 그대로:

```
<<macro result: advance_story ran to the end; 21 action(s) reached the game; on scene MapScene at the end.>>
advance_story ran to the end; 21 action(s) reached the game; on scene MapScene at the end.

Text the game showed while the macro ran (37 line(s), oldest first):
  AdvancePrompt: "Click / Any key - Next"
  AdvancePrompt: "Click / Any key - Show all"
  ChatText: "Word's grandfather, Sans, had once been a famous adventurer, and Word grew up hearing his many tales of adventure."
  …
```

agent 는 step 3 을 "모든 대사 출력(플레이스홀더/깨진 텍스트 없음)" 으로 보고했다. CLEAN 빌드라 맞다. `OBS-2` 빌드로는 돌리지 않았다.

## Code Walkthrough

- `app/qa/shown_text.py` — 기록과 `mark`/`since`.
- `app/qa/channel.py` `on_pulse` — `PulseReading` 을 한 번 파싱해 두 곳에 준다.
- `app/agents/qa/tools/macro_tools.py` `render_shown_text`, `_render`, `_drive` — 답에 싣고 위치를 옮긴다. `resume_macro` 답은 멈춘 뒤에 뜬 것만 싣는다.
- `app/agents/qa/macro/session.py` `text_mark`.
- `app/prompts/qa_run/v28/skill_macro.md`, `app/agents/qa/arch.py`.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 스택 맨 위 2927 passed
- [x] 새 테스트 `tests/test_qa_macro_shown_text.py` 6개 (타자 효과, 상한, `run_macro` 왕복), `test_v28_*`
- [ ] `OBS-2` 빌드 3런 — 하지 않음 (예산)

### 실제 런 (스택 맨 위 `2200d76` 로 1회)

이 스택 다섯 PR(ARTEL-1220, 1219, 1221, 1222, 1223)을 모두 얹은 branch 로 한 번 돌렸다. 각 변경의 몫을 따로 가르지 못한다.

- 시나리오: WordVenture L1 (test run 4, 24 step), CLEAN 빌드 `WordVentureEvalClean1`, eval stack `artel_eval_20261022`
- 축: model `claude/claude-sonnet-5-5`, `contentMapMode=frozen`, `knowledgeMode=off`, `screen_capture=on_demand`, 게임 하나씩
- 채점: `.agents/skills/qa-run-local/SKILL.md` 의 dedupe SQL (`B-seeded-defects/sql/score.sql`), 거짓 기대값은 step 5, 8, 11, 12, 14

| qa_try | `agent_arch` | `prompt_version` | `agent_fingerprint` | 판정 | 일치 /24 | 거짓 기대값 잡음 /5 | 오탐 | 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 246 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 19 | 1 (12) | 1 (19) | $4.89 |
| 249 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 20 | 1 (12) | 0 | $4.42 |
| 252 (후) | `v32-performance` | `v29` | `04952982dbf6` | 24 | 16 | **3 (5, 11, 12)** | 6 (18, 19, 21–24) | $6.15 |

읽는 법:

- **1회다.** 결과가 아니라 방향이다. `prompt_version` 도 함께 움직였다 — prompt 가 이 변경의 일부라 같게 둘 수 없었다.
- 거짓 기대값은 1 → 3. step 5 와 11 은 `performed=true`, `passed=false` 로 왔다 ("expected: 오른쪽 방향키로 다음 노드로 이동 / observed: … Stage 0 유지"). step 8 과 14 는 여전히 통과였다. 둘 다 agent 가 `expected:` 줄에 기대값이 아니라 행동을 적었다 ("expected: spell 카드만 슬롯에 있는 상태에서 발동 버튼이 눌릴 수 있는지 확인"). 이슈 목표(4 이상)에는 못 미친다.
- 오탐 6개는 step 18 이후다. 이 런에서 action tool 답 34개가 "The game reported no result" 였고 (전 두 런은 0), orchestration log 에서 `ACTION_RESULT` 가 30~60초 늦게 도착한 것이 보인다. agent 는 step 21~24 를 "입력이 수십 초 지연되어 진행 불가", `SETUP-FAILED` 로 적었다. 게임 쪽 지연이고 agent-server 가 결과 도착 시각을 바꿀 길은 없다고 보지만, 이 런 하나로는 변경과 떼어 증명하지 못한다.
- ARTEL-1221: step 1, 4, 7 에서 `capture_screen` 을 "첫 캡처", "맵 씬 첫 캡처", "전투 씬 첫 캡처" 로 불렀다. 전 두 런은 step 1 에서 찍지 않았다. CLEAN 빌드라 잡을 시각 결함은 없었다.
- ARTEL-1222: `advance_story` 답에 37줄이 실렸다. `AdvancePrompt` 의 "Click / Any key - Next/Show all" 이 대사 사이마다 끼어 줄 수의 절반가량을 차지한다.
- ARTEL-1219: 이 SDK 는 아직 `errors` 를 안 보내므로 `unseen errors` 줄이 나올 일이 없었다.
- ARTEL-1223: agent 가 step 15 에서 `get_performance` 를 한 번 불렀고 "orchestration answered 404" 를 받았다. eval orchestration 에 endpoint 가 아직 없다.
- `MAX_LOGGED_CHARS` 가 tool 결과를 4,000자에서 자르므로 tool 결과 끝에 붙는 줄이 `qa_log` 에 없는 것은 근거가 되지 않는다.

## Risks

- `AdvancePrompt` 같은 UI 안내 글자가 대사 사이마다 끼어 줄 수가 두 배 가까이 된다. 줄 수가 40을 넘으면 뒤의 대사가 잘린다. 같은 출처의 글자가 번갈아 바뀌는 것을 거르는 일은 다음 손이다.

## Rollback

이 PR 의 커밋을 revert 한다.

Jira: ARTEL-1222

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #266: feat(qa): get_performance tool 과 지난 10초 hitch 수 한 줄 추가</summary>

## Why

SDK 는 1초마다 `PERFORMANCE` (frame time, fps, hitch 수, CPU, 메모리)를 보내고 orchestration 이 `SdkPerfIngestService` 로 저장하지만, `artel-agent-server/app` 에는 그것을 읽는 코드가 없어 성능 결함을 판정할 길이 없었다 (`.work/eval-feedback-20261022/D-support-scope/results.md` 5절).

## What Changed

- `PerformanceReader` (`app/qa/performance.py`)가 `GET {ORCHESTRATION_BASE_URL}/internal/qa-tries/{qaTryId}/performance?sinceFrame=&lastSeconds=` 를 묻는다. 응답은 `{"samples": [{frame, at, frameTimeMs, fps, hitchCount, cpuPercent, memoryMb}]}`. orchestration 쪽 endpoint 는 orchestration #367~#371 로 들어갔다. 그쪽의 `frame` 은 가장 가까운 `pulse`·action frame 에서 어림한 값이고 없으면 0 이라, 0 은 frame 범위에서 뺀다. 창을 안 주면 orchestration 은 지난 60초를 주지만 이쪽은 늘 창을 보낸다.
- 새 tool `get_performance(since_frame, last_seconds)` — 창 하나의 평균과 p95 frame time, 최저 fps, hitch 수, 메모리 증가, CPU 평균. 창을 안 주면 지난 10초.
- action 뒤와 `observe_scene` 에서 많아야 5초마다 지난 10초 hitch 수를 다시 읽고, 0 이 아니면 tool 결과 끝에 "performance: N hitch(es) in the last 10 s — …" 한 줄. 묻다가 실패하면 60초 쉰다.
- 성능 이상은 step 판정이 아니라 `report_issue` 로 보내라는 문장을 그 줄과 tool 답에 둔다.
- prompt `qa_run/v29`: `tool_get_performance.md`.
- `QA_ARCH_LABEL` → `v32-performance`, fingerprint → `04952982dbf6`.

## Example

`tests/test_qa_performance.py` 의 `httpx.MockTransport` fixture (실제 orchestration 없음): 표본 3개 (`frameTimeMs` 16.6, 110, 105, `hitchCount` 0, 9, 8, `memoryMb` 510→560)에 대해 `get_performance` 답:

```
Performance over the last 10 s (3 one-second sample(s), frames 600–670):
- frame time: average 77.2 ms, p95 110.0 ms
- lowest fps: 9.0
- hitches: 17
- memory: 510 MB → 560 MB (+50 MB)
- CPU: average 50%
```

실제 런에서는 eval orchestration 에 endpoint 가 없어 "Performance data could not be read — orchestration answered 404." 를 받았다.

## Code Walkthrough

- `app/qa/performance.py` — wire 모델, 요약, 읽는 쪽과 그 간격.
- `app/agents/qa/tools/tool_context.py` `performance`, `_act`, `answer` — 런마다 reader 하나, action 뒤 갱신, 한 줄.
- `app/agents/qa/tools/observation_tools.py` `get_performance`.
- `app/agents/qa/tools/phase.py` — `ANY_PHASE`.
- `app/prompts/qa_run/v29/`, `app/agents/qa/arch.py`.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 2927 passed
- [x] 새 테스트 `tests/test_qa_performance.py` 11개, `test_v29_*`
- [ ] 매 frame 100ms sleep 빌드 3런 — 하지 않음 (endpoint 미배포, 예산)

### 실제 런 (스택 맨 위 `2200d76` 로 1회)

이 스택 다섯 PR(ARTEL-1220, 1219, 1221, 1222, 1223)을 모두 얹은 branch 로 한 번 돌렸다. 각 변경의 몫을 따로 가르지 못한다.

- 시나리오: WordVenture L1 (test run 4, 24 step), CLEAN 빌드 `WordVentureEvalClean1`, eval stack `artel_eval_20261022`
- 축: model `claude/claude-sonnet-5-5`, `contentMapMode=frozen`, `knowledgeMode=off`, `screen_capture=on_demand`, 게임 하나씩
- 채점: `.agents/skills/qa-run-local/SKILL.md` 의 dedupe SQL (`B-seeded-defects/sql/score.sql`), 거짓 기대값은 step 5, 8, 11, 12, 14

| qa_try | `agent_arch` | `prompt_version` | `agent_fingerprint` | 판정 | 일치 /24 | 거짓 기대값 잡음 /5 | 오탐 | 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 246 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 19 | 1 (12) | 1 (19) | $4.89 |
| 249 (전) | `v27-game-functions` | `v24` | `7d56243753d1` | 24 | 20 | 1 (12) | 0 | $4.42 |
| 252 (후) | `v32-performance` | `v29` | `04952982dbf6` | 24 | 16 | **3 (5, 11, 12)** | 6 (18, 19, 21–24) | $6.15 |

읽는 법:

- **1회다.** 결과가 아니라 방향이다. `prompt_version` 도 함께 움직였다 — prompt 가 이 변경의 일부라 같게 둘 수 없었다.
- 거짓 기대값은 1 → 3. step 5 와 11 은 `performed=true`, `passed=false` 로 왔다 ("expected: 오른쪽 방향키로 다음 노드로 이동 / observed: … Stage 0 유지"). step 8 과 14 는 여전히 통과였다. 둘 다 agent 가 `expected:` 줄에 기대값이 아니라 행동을 적었다 ("expected: spell 카드만 슬롯에 있는 상태에서 발동 버튼이 눌릴 수 있는지 확인"). 이슈 목표(4 이상)에는 못 미친다.
- 오탐 6개는 step 18 이후다. 이 런에서 action tool 답 34개가 "The game reported no result" 였고 (전 두 런은 0), orchestration log 에서 `ACTION_RESULT` 가 30~60초 늦게 도착한 것이 보인다. agent 는 step 21~24 를 "입력이 수십 초 지연되어 진행 불가", `SETUP-FAILED` 로 적었다. 게임 쪽 지연이고 agent-server 가 결과 도착 시각을 바꿀 길은 없다고 보지만, 이 런 하나로는 변경과 떼어 증명하지 못한다.
- ARTEL-1221: step 1, 4, 7 에서 `capture_screen` 을 "첫 캡처", "맵 씬 첫 캡처", "전투 씬 첫 캡처" 로 불렀다. 전 두 런은 step 1 에서 찍지 않았다. CLEAN 빌드라 잡을 시각 결함은 없었다.
- ARTEL-1222: `advance_story` 답에 37줄이 실렸다. `AdvancePrompt` 의 "Click / Any key - Next/Show all" 이 대사 사이마다 끼어 줄 수의 절반가량을 차지한다.
- ARTEL-1219: 이 SDK 는 아직 `errors` 를 안 보내므로 `unseen errors` 줄이 나올 일이 없었다.
- ARTEL-1223: agent 가 step 15 에서 `get_performance` 를 한 번 불렀고 "orchestration answered 404" 를 받았다. eval orchestration 에 endpoint 가 아직 없다.
- `MAX_LOGGED_CHARS` 가 tool 결과를 4,000자에서 자르므로 tool 결과 끝에 붙는 줄이 `qa_log` 에 없는 것은 근거가 되지 않는다.

## Risks

- orchestration 의 endpoint 가 배포되기 전에는 tool 이 404 를 말하고 한 줄은 나오지 않는다.
- action 뒤 갱신이 최대 2초 (요청 timeout) 를 더할 수 있다. 5초 간격과 실패 뒤 60초 쉼으로 묶었다.

## Rollback

이 PR 의 커밋을 revert 한다.

Jira: ARTEL-1223

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #267: fix(llm): 기능별 model 설정에서 effective 값 읽기</summary>

## Why

orchestration #367~#371 이 `GET /internal/settings/models` 의 응답을 바꿨다. `scenarioAuthoringModel` 과 `decisionModel` 은 관리자가 저장한 값 (null 가능)이 되고, 실제로 쓸 값은 `effective` 아래에 실린다. 이 server 는 위의 두 값만 읽고 있어서, 저장값이 null 이고 기본값이 쓰여야 하는 경우 Jev 로 떨어진다.

## What Changed

- `effective` 가 있으면 `effective.decisionModel`, `effective.scenarioAuthoringModel` 을 읽는다.
- `effective` 가 없는 옛 orchestration 이면 종전대로 위의 두 값을 읽는다.

## Example

`tests/test_llm_model_settings.py` 의 `httpx.MockTransport` fixture:

```json
{"scenarioAuthoringModel": null, "decisionModel": null,
 "effective": {"scenarioAuthoringModel": "openai/gpt-6-luna", "decisionModel": "claude/claude-haiku-5-5"}}
```

→ `get_decision_model()` 이 `claude/claude-haiku-5-5`. `effective.decisionModel` 이 null 이면 저장값이 있어도 null (Jev) 이다. 실제 orchestration 과는 맞대 보지 않았다.

## Code Walkthrough

- `app/llm/model_settings.py` `_ModelSettingsBody.in_use` — `effective` 우선, 없으면 최상위 두 값.

## Validation

- [x] `LANGSMITH_TRACING=false python -m pytest -q` — 2886 passed
- [x] 새 테스트 2개

## Risks

- `effective` 가 있고 그 안의 값이 null 이면 저장값으로 내려가지 않는다. orchestration 이 기본값까지 채워 보내는 계약을 따른 것이다.

## Rollback

이 커밋을 revert 한다.

🤖 Generated with [Claude Code](https://claude.com/claude-code)


</details>

<details><summary>원본 PR #268: feat(agent): 시연 검증 절차 추출과 QA context 추가</summary>

## Why

사람이 TC 검증을 시연하고 결과를 판정해도 이후 QA가 그 절차를 사용할 context가 없었다. 시연 원본에서 검토 가능한 절차 초안을 만들고, backend가 승인한 snapshot을 QA 실행에 전달한다.

## What Changed

- `POST /internal/demonstrations/extract`는 기존 structured LLM provider로 event·image를 읽고 절차·assertion·사실·추론·질문을 생성한다. 게임 도구는 연결하지 않는다.
- schema와 원본 event ID를 검증한다. PNG는 서버에서 제한된 host·크기·timeout으로 읽어 provider에 data URL로 전달한다.
- 사람은 outcome/evidenceEventId만 남길 수 있다. 설명은 선택 hint이며 기대 결과와 출력 언어는 TC를 우선한다. TC에 없는 정밀 latency를 필수 질문으로 만들지 않는다.
- `QaCaseRef.verification_procedure`는 선택 필드다. 기본 agent와 planner-executer의 ORDERED/FREE context에 승인 snapshot과 현재 실행 evidence 사용 지시를 첨부한다.
- arch는 `v28-demonstration-procedures`, `pe3-demonstration-procedures`로 올리고 안내 prompt hash를 run_config에 기록한다. 승인 절차가 없으면 기존 계획 문자열을 유지한다.

## Example

실제 로컬 Unity 버튼 카운터와 backend에서 사람이 판정·화면만 남긴 시연을 추출해 procedure `3`, revision `1`을 승인했다. backend가 이 snapshot을 TC 1의 verification_procedure로 보냈고, FREE 정상 run 2의 report_case는 절차 3/revision 1을 명시하면서 reset → Count: 0 → Increment 클릭 → Count: 1이라는 현재 실행 관찰로 PASS를 기록했다.

실패 fixture run 3/4에서는 같은 승인 절차가 있어도 클릭 후 Count: 0이 유지되는 현재 evidence를 기준으로 FAIL과 MAJOR issue를 보고했다. 과거 시연의 PASS를 현재 결과로 복사하지 않았다. backend qa_try.run_config에는 `demonstration/v1/qa_context` hash와 실행 축이 남고 qa_log에는 report_step/report_case/finish_run이 기록된다.

이 결과는 실제 provider·SDK·backend를 연결한 단순 fixture의 조건별 1회 검증이다. 변경 전 동일 축 비교와 planner-executer의 실제 게임 검증은 하지 않았으므로 품질 향상이나 일반 게임 정확도를 주장하지 않는다.

## Code Walkthrough

- `app/demonstrations/schemas.py:10` — 추출 요청·응답과 승인 절차의 typed 계약, 근거 참조·길이·미해결 질문을 검증한다.
- `app/demonstrations/agent.py:27` — 원본과 제한된 PNG를 실제 structured provider에 전달하고 sourceHash 및 근거 ID를 검증한다.
- `app/api/demonstrations.py:14`, `app/main.py:150` — 내부 endpoint와 120초 timeout, provider 오류 응답을 연결한다.
- `app/prompts/demonstration/v1/system.md:6`, `app/prompts/demonstration/v1/qa_context.md:6` — TC 기대·언어 우선, 선택 annotation, 사실/추론 구분과 현재 실행 판정을 지시한다. prompts-lock에 hash를 고정했다.
- `app/qa/schemas.py:27`, `app/qa/service.py:153` — 선택 snapshot을 수용하고 사용한 안내 prompt hash를 실행 설정에 남긴다.
- `app/agents/qa/runner.py:243`, `app/agents/qa/arch.py:432` — ORDERED TC 첫 단계와 FREE case에 승인 절차를 붙이며 arch label을 변경한다.
- `app/agents/planner_executer/prompts.py:81`, `app/agents/planner_executer/arch.py:1` — planner와 evaluator가 읽는 case context에도 같은 snapshot과 지시를 전달한다.
- `tests/test_demonstrations.py:31` — 추출 원본·실패·이미지·선택 annotation·두 engine context와 기존 계약 호환을 검증한다.
- `.plan/general/ARTEL-1261-demonstration-extraction.md:1`, `.plan/general/ARTEL-1261-llm-smoke.md:1` — 실제 QA와 LLM smoke의 결과·범위·한계를 기록한다.

## Validation

- [x] 전체 회귀 2,909 PASS, Windows 경로 비교 문제인 `test_the_parser_is_the_only_place_a_definition_is_built` 1개 제외
- [x] 마지막 schema/prompt 변경 후 `python -m pytest tests/test_demonstrations.py tests/test_prompts_loader.py tests/test_prompts_lock.py -q`: 95 PASS
- [x] `python -m app.prompts.lock --write`, `git diff --cached --check`
- [x] 실제 OpenRouter gpt-6-luna 추출 4회 및 실제 UI 최소 입력 시연에서 승인 절차 생성
- [x] 실제 Unity 정상/실패 fixture ORDERED/FREE 각각 1회
- [x] 독립 검토 PASS

| QA run | mode | fixture | 결과 | 소요 | LLM calls | 비용 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | ORDERED | 정상 | 1/1 PASS | 37.110초 | 12 | $0.007502 |
| 2 | FREE | 정상 | 1/1 PASS | 50.619초 | 10 | $0.007083 |
| 3 | ORDERED | 실패 | 0/1, issue 1개 | 30.875초 | 9 | $0.003408 |
| 4 | FREE | 실패 | 0/1, issue 1개 | 54.294초 | 10 | $0.003766 |

공통 축: model `openai/gpt-6-luna`, prompt `v24`, agent_arch `v28-demonstration-procedures`, knowledge frozen, content map/cheat/behavior off. fingerprint는 ORDERED `7d56243753d1`, FREE `6399537c5953`이며 mode별 도구가 다르다. run 1은 procedure 2/revision 2, 나머지는 procedure 3/revision 1을 사용했다. 원본 응답·tool 보고·사용량은 gitignored `.venv/demo-qa-*`에 보관했고 token은 포함하지 않았다.

## Risks

SDK producer [artel-sdk#169](https://github.com/project-artel/artel-sdk/pull/169)와 이 agent producer를 backend ARTEL-1259보다 먼저 반영해야 한다. endpoint는 내부 네트워크용이며 evidence 저장소가 agent에서 접근 가능해야 한다. 실제 게임별 판정 품질은 별도 반복 측정이 필요하다. 변경 전 baseline과 planner-executer 실제 실행은 미검증이다.

## Rollback

backend에서 시연 추출·QA 연결을 끄고 이 변경을 되돌린다. verification_procedure가 없는 기존 요청은 계속 처리할 수 있다. 저장된 원본·승인 절차의 관리와 철회는 backend가 담당한다.

Jira: ARTEL-1261


</details>

<details><summary>원본 PR #275: feat(qa): TC 검증 시도와 사용자 질문 대기 추가</summary>

## Why

step은 TC 검증 방법을 참고하는 절차인데 기존 agent는 step마다 판정을 보고하고 완료해야 했다. TC 검증 시작·결과를 명시적으로 기록해 영상 구간과 사람 평가를 연결하고, 진행 방법이 불분명하면 사용자의 첫 답변까지 QA를 멈춘다.

선행 agent PR #270 위에 쌓는 변경이다. [서버 PR #383](https://github.com/project-artel/artel-orchestration-server/pull/383)의 merge·API 배포 이후 적용한다.

## What Changed

- `report_step`·`report_case`를 노출하지 않고 `start_test_case`·`report_test_case`로 검증 시도를 기록한다. 반복 검증은 새 시도이며 미검증 TC는 실패로 바꾸지 않는다.
- 같은 TC의 사람 평가와 당시 snapshot을 context에 제공한다. 피드백은 참고 자료이며 별도 사용자 승인 절차를 추가하지 않는다.
- `ask_user`가 첫 답변까지 전체 tool 진행을 멈추고, 대기 시간을 QA 실행 제한에서 제외한다. 취소는 대기를 끝낸다.
- 원본 보고 payload를 고정해 응답 유실 후에도 같은 UUID·결과로 재시도한다. 시도별 macro 조작과 knowledge 인용 거절 수도 보존한다.
- QA 구조 `v28-case-evaluation`, prompt `qa_run/v26`을 추가하고 기존 버전은 보존한다.

## Example

계약 테스트에서 agent가 같은 TC를 두 번 검증하면 서로 다른 attempt UUID가 기록된다. 첫 결과 보고의 응답이 유실되면 새 시도를 만들지 않고 처음 보낸 payload를 재전송한다. 사용자가 `ask_user`에 답하기 전에는 다음 조작 tool이 실행되지 않으며, 답변 뒤 이어서 실행한다.

이 예시는 자동 계약 테스트의 결과다. 실제 게임에서 오탐률·미탐률이 개선됐다는 측정 결과는 아니다.

## Code Walkthrough

- `app/agents/qa/tools/reporting_tools.py:23` — TC 시작·보고·사용자 질문 tool을 구성한다.
- `app/qa/evaluation.py:26` — 질문 대기 시간을 실행 제한에서 제외하고 내부 API를 재시도한다.
- `app/agents/qa/tools/state.py:1`, `app/agents/qa/runner.py:1` — 검증 시도별 상태와 TC 단위 완료 조건을 유지한다.
- `app/agents/qa/compaction.py:1`, `app/agents/qa/context.py:1` — 압축 후에도 TC 시도와 피드백을 보존한다.
- `app/agents/qa/macro/lift.py:1`, `app/agents/qa/tools/macro_tools.py:1` — 반복 TC의 조작을 해당 시도의 macro 기록에 연결한다.
- `app/prompts/qa_run/v26/system.md:1`, `app/prompts/prompts-lock.json:1` — TC 단위 지침과 prompt fingerprint를 고정한다.
- `tests/test_qa_case_evaluation.py:1` — 멱등 보고·반복 시도·질문 대기·취소 경로를 검증한다.
- `docs/qa-tc-evaluation-verification.md:1` — 검증 결과와 실제 게임 비교 미실행 사유를 기록한다.

## Validation

- [x] 전체 pytest 2,889개 통과
- [x] TC·knowledge·behavior 집중 테스트 149개 통과
- [x] context·prompt 집중 테스트 179개 통과
- [x] 마지막 prompt 정리·lock 재생성 후 집중 테스트 85개 통과
- [ ] 실제 Unity QA 변경 전·후 비교: LLM 복호화 설정이 없어 미실행

## Risks

새 내부 API가 있는 서버를 먼저 배포해야 한다. 사용자 질문은 직접 종료할 때까지 무기한 대기하므로 실행 자원은 유지된다. 자동 테스트 통과를 agent 정확도 개선으로 해석하지 않는다.

## Rollback

이전 agent 버전으로 되돌린다. 서버의 additive 테이블과 기존 판정·평가 이력은 유지한다.

Jira: ARTEL-1270


</details>
