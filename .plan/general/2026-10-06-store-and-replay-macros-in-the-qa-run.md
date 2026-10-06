# 2026-10-06 — macro 를 저장하고 다시 부르는 다섯 조각

- Date: 2026-10-06
- Jira: ARTEL-917, ARTEL-918, ARTEL-920, ARTEL-923, ARTEL-925
- Status: Reviewed

## Goal

QA agent 가 성공한 조작 묶음을 macro 로 적어 두고 다시 부를 수 있게 한다. 다섯 조각을
한 branch 에 넣는다.

| 이슈 | label | 내놓는 것 |
| --- | --- | --- |
| ARTEL-917 | `macro-source-parser` | `ast.parse` 로 읽고 허용 node 만 통과시키는 parser |
| ARTEL-918 | `macro-definition-model` | 통과한 tree 의 pydantic 모델과 JSON |
| ARTEL-920 | `macro-find-binding` | `find`·`selector` 를 `PulseMemory` 로 푸는 조회와 평가 |
| ARTEL-923 | `macro-runner` | statement 를 batch 로 쪼개 `ctx.run` 을 반복 호출하는 loop |
| ARTEL-925 | `macro-tools` | tool 다섯 (`write_macro`·`edit_macro`·`read_macro`·`register_macro`·`run_macro`) |

## Non-goals

- `eval` 과 `exec`. 설계 전체가 둘을 쓰지 않는 것 위에 선다.
- `artel-orchestration-server` 의 macro 저장 frame (`ARTEL-921`, `ARTEL-919`).
  `register_macro` 가 쓸 frame 이 아직 없으므로 **등록된 macro 도 이 PR 에서는 런의
  상태에 산다.** 그 자리는 `MacroBook.register` 하나다.
- `QA_ARCH_LABEL` bump. `ARTEL-926` 의 몫이다. tool 다섯이 늘어 `arch_fingerprint` 는
  움직이므로 `tests/test_qa_arch.py` 의 pin 두 개(`_EXPECTED_DEFAULT_TOOL_NAMES`,
  `_EXPECTED_DEFAULT_FINGERPRINT`)만 고치고 label 은 둔다.
- 값을 돌려주는 helper, `return`, `for`·`while`, `and`·`or`·`not`, attribute access.
- 관계를 빼는 길. `register_macro` 는 `screen` 관계를 더하기만 한다.
- `action_tools.py` 를 고치는 것. 열여섯 tool 의 `JsonRpcAction` 조립은 지금 자리에
  그대로 두고, macro 쪽은 `grammar.py` 의 표가 따로 든다. 둘이 어긋나지 않는지는
  테스트가 본다(Step 6).

## Context / Constraints

- 기존 소비자를 깨뜨리지 않는다. `QaRunState.remember_dispatch` 와
  `dispatched_action_params` 는 `capability_tools.py` 의 `_action_record` 가 읽는다.
  뜻을 바꾸지 않고 옆에 더한다.
- `ToolContext.run`(`app/agents/qa/tools/tool_context.py:67`)은 `JsonRpcAction` 배열을
  한 번에 보내고 그 뒤 pulse 를 돌려준다. 중간에 끼울 수 없으므로 action statement
  하나가 batch 하나다.
- 같은 batch 안의 action 은 실패해도 끝까지 간다(`ArtelManager.ExecuteActionRequest`).
  그래서 한 batch 에 action 둘을 담으면 `pending` 이 거짓말이 된다.
- `PulseMemory` 가 유일한 출처다. `SendsGameState` 기본값이 꺼짐이라 `SceneMemory` 에
  걸면 기본 빌드에서 한 번도 안 돈다. `observable()` 만 `SceneMemory.observables` 를
  읽는다.
- `PulseObject.key`(`app/qa/pulse.py:172-174`)는 `씬/(selector 또는 path)` 다.
  `under=` 는 이 key 에 prefix 로 맞추고, object 의 같음도 이 key 로 가른다.
- SDK 의 `Number` 가 `Math.Round(value, 4).ToString("0.####")` 로 쓰므로 float `3.0` 이
  `3` 으로 도착한다. 선언 타입은 `int` 와 `float` 를 가르지만 연산자 dispatch 는 둘을
  number 한 행으로 합친다. 두 층을 섞지 않는다.
- 에러 코드에 `MACRO_` 접두를 붙이지 않는다. 다만 저장 시점 거절은
  `MACRO_NODE_REJECTED` 다.
- **import 방향이 한쪽이다.** `app/agents/qa/macro/` 는 `app/agents/qa/tools/` 를 import
  하지 않는다. `app.agents.qa.tools.tool_context` 를 import 하면 `tools/__init__.py` 가
  먼저 돌고 그것이 `macro_tools` 를 import 하므로, 반대 방향을 열면 순환이 된다.
  runner 는 아래 Step 4 의 `MacroHost` protocol 만 받는다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 읽었다. `action_tools.py`(tool 열여섯, `parse_target`),
  `tool_context.py`(`run`·`answer`), `state.py`(`QaRunState`), `pulse.py`
  (`PulseObject`·`PulseMemory`·`PulseMember`), `scene.py`(`SceneMemory.observables`),
  `capability_tools.py`(`_action_record`·`_rationale_problem`), `reporting_tools.py`
  (`finish_run`·`unreported_steps`), `screen.py`·`capability.py`(tool 설명이 사는 자리),
  `arch.py`·`tests/test_qa_arch.py`(tool 목록과 fingerprint pin).

- [ ] **Step 1: ARTEL-917 — parser** (`app/agents/qa/macro/`)
  - `errors.py` — `MacroRejection`(저장 시점 거절, code `MACRO_NODE_REJECTED`) 과
    `MacroFailure`(실행 시점). 코드 일곱: `REQUIRE_FAILED`·`SCENE_MISMATCH`·
    `ACTION_REJECTED`·`SELECTOR_NOT_FOUND`·`SELECTOR_AMBIGUOUS`·`STALE_BINDING`·
    `COMPARISON_REJECTED`. 네 모듈(parser·binding·runner·tools)이 같이 쓰므로 모듈을
    따로 둔다.
  - `grammar.py` — 데이터만 든다.
    - 선언 타입 다섯(`int`·`float`·`string`·`bool`·`object`), 거절되는 두 이름
      (`vector2`·`vector3`).
    - shape 일곱(number·string·bool·vector2·vector3·object·other), 연산자 여섯,
      `(shape, operator) → 허용/거절` 표.
    - tool 열넷의 signature: macro 쪽 파라미터 이름과 순서, 각 파라미터의 선언 타입,
      어느 자리가 target 인지, **그리고 `JsonRpcAction` 의 `method` 와 인자 순서.**
      runner 가 이 표만 보고 배치를 만든다.
    - reader 호출 여덟의 signature 와 반환 shape(`scene`→string, `text`→string,
      `actionable`·`exists`·`absent`→bool, `static`·`observable`·`member`→unknown).
  - `parser.py` — `ast.parse` 로 읽고 허용 node 만 통과시킨다.
    - `def` 는 최상위만. **진입점은 이름이 macro 이름과 정확히 같은(대소문자까지)
      최상위 `def` 하나다.** 없으면 거절하고, 같은 이름의 `def` 가 둘이면 거절한다.
      나머지 최상위 `def` 가 helper 다.
    - 이름 묶기는 `def` 하나 안에서 frame stack 으로 센다. `if` 몸통과 `else` 몸통은
      각각 자기 frame 이라 형제 분기가 같은 이름을 묶는 것은 되고, 몸통 안에서 묶은
      이름을 몸통 밖에서 쓰는 것은 거절된다.
    - 재귀(자기 호출·상호 재귀)와 호출 깊이 3(진입점 포함) · statement 총수 128 을
      저장 시점에 정적으로 판정한다. 세기: action/require/대입/flag/ask_verdict 는 1,
      `if` 는 `1 + max(두 가지)`, helper 호출 statement 는 `1 + cost(helper)`.
    - 거절 문장은 받을 수 있는 것을 이름으로 전부 댄다.
  - **parser 가 통과시키면서 바로 frozen 모델 node 를 만든다.** tree 를 두 번 걷지
    않는다 — 검사용 walk 와 변환용 walk 를 따로 두면 허용 node 목록이 두 벌이 되고
    언제나 한쪽만 고쳐진다.

- [ ] **Step 2: ARTEL-918 — model** (`app/agents/qa/macro/model.py`)
  - 타입 선언만 든다. `MacroDefinition`(이름·원본 텍스트·진입점 하나·helper 여럿),
    `MacroFunction`, `MacroParameter`, statement 일곱 종(한 목록에 섞고 `kind` 로
    가른다), `MacroCondition`·`MacroComparison`·`MacroOperand`·`MacroReaderCall`,
    `MacroTarget`.
  - 타입 이름 enum 은 이슈대로 일곱이다(`vector2`·`vector3` 포함). 선언 자리에서는
    parser 가 둘을 거절하므로 실제로 모델에 실리는 것은 다섯뿐이고, 그 사실을 주석에
    적는다.
  - 전부 `frozen=True`, 순서 있는 것은 `tuple`. JSON round-trip 으로 선언 타입이 남는다.
  - **유일한 생성 경로는 `macro_definition_from_source(name, source)` 다.** 그 함수는
    `parser.py` 가 들고 `model.py` 가 다시 내보낸다. JSON 만 고치는 경로를 만들지 않고,
    tool 도 runner 도 `model_validate` 로 정의를 만들지 않는다.
  - statement 마다 `ast.unparse` 결과를 `text` 로 싣는다. runner 가 `ctx.run` 의
    `summary` 에 쓸 값이고, 실행이 저장된 JSON 만 보게 하려면 여기 있어야 한다.

- [ ] **Step 3: ARTEL-920 — find binding** (`app/agents/qa/macro/binding.py`)
  - `find(label=·name=·under=)` 를 `PulseMemory.held` 에 대고 푼다. 하나도 없으면
    `SELECTOR_NOT_FOUND`, 둘 이상이면 `SELECTOR_AMBIGUOUS`, pulse 가 한 장도 없으면
    `ACTION_REJECTED`.
  - 묶인 이름이 드는 값은 id 하나가 아니라 기록 자체다. 묶은 뒤 그 기록이 더는 안
    잡히면 `STALE_BINDING`.
  - `selector(...)` 를 묶은 이름은 그 자리에서 풀지 않고 문자열로 둔다(late binding).
  - reader 여덟의 평가. `actionable()` 은 `offers` 로만 답한다. `observable()` 만
    `SceneMemory.observables` 를 읽고 나머지는 `PulseMemory` 를 읽는다.
  - `shape_of(value)` 와 `(shape, operator)` 표 조회. 거절은 `COMPARISON_REJECTED`,
    payload 가 도착한 모양·건 연산자·그 모양에서 되는 연산자를 댄다. object 의 같음은
    pulse key 로 가른다.
  - 이 모듈이 네 가지를 맡는다(조회·stale 판정·reader 평가·shape 와 표 조회). 네
    가지가 전부 "도착한 값" 하나를 두고 하는 일이라 한 모듈에 둔다. reader 가 늘어
    읽히지 않게 되면 그때 가른다.

- [ ] **Step 4: ARTEL-923 — runner** (`app/agents/qa/macro/runner.py`)
  - **`MacroHost` protocol 두 개를 받는다.** `run(actions, summary, step) -> str` 과
    `memories() -> (PulseMemory, SceneMemory)`. `ToolContext` 를 import 하지 않는다 —
    위 Constraints 의 import 방향 때문이고, 그래야 runner 테스트가 가짜 channel 없이
    돈다. 묶는 자리는 `macro_tools.py` 하나다.
  - `applied` 는 **게임에 나간** action statement 다. `ctx.run` 이 돌려주는 문자열을
    파싱하지 않는다 — 이슈가 `applied` 를 "이미 나간" 으로 정의하므로 보냈다는 사실이
    곧 기준이고, 개별 action 의 성공 여부는 agent 가 결과 문장에서 읽는다.
  - `def` 마다 statement 를 1부터 세고(중첩 `if` 몸통까지 깊이 우선으로 센다), action
    statement 하나가 batch 하나다.
  - `applied`·`pending`·`skipped` 를 `def` 이름과 함께 싣는다. helper 호출은 펼치지
    않고 호출로 돌고, payload 가 호출 사슬을 싣는다.
  - `flags` 와 `verdict_requests` 를 따로 거두고 각 항목에 `observed`(감싼 `if` 조건의
    reader 호출 값 전부)를 붙인다.
  - `require` 실패는 `applied` 가 비면 `SCENE_MISMATCH`, 하나라도 나갔으면
    `REQUIRE_FAILED`.
  - `enter_text` 는 묶인 기록의 `id` 를 `target_id` 자리에 넣고, `None` 이면 게임에
    아무것도 보내기 전에 `ACTION_REJECTED`.
  - `ctx.run` 의 `summary` 에 statement 의 `text` 를 넣고, `step` 은 `run_macro` 호출이
    받은 `step` 을 그대로 쓴다. macro 안의 statement 가 시나리오 step 을 따로 가질 길이
    없고, `thought` 가 호출당 하나뿐인 것과 같은 사정이다.

- [ ] **Step 5: ARTEL-925 — tools** (`app/agents/qa/tools/macro_tools.py`)
  - `MacroBook`(`app/agents/qa/macro/book.py`) — 초안·읽은 기록·등록된 정의를 한 객체가
    든다. `write`·`read`·`edit`·`register`·`registered` 다섯 메서드이고, tool 없이
    단위 테스트가 된다. `QaRunState` 에는 `macros: MacroBook` 한 칸만 더한다.
  - **`MacroBook.register` 가 좁혀 둔 저장 자리다.** 지금은 메모리에 넣고, `ARTEL-921`
    이 frame 을 내놓으면 그 한 메서드만 바뀐다.
  - `build_macro_tools(ctx) -> list[BaseTool]` 를 내보내고,
    `app/agents/qa/tools/__init__.py` 의 목록에서 `build_action_tools` 뒤 ·
    `build_reporting_tools` 앞에 넣는다. 목록의 순서가 계약이다.
  - `write_macro(step, thought, name, source)` — 파싱만. 진입점 `def` 가 없으면 거절.
  - `edit_macro(step, thought, name, old_text, new_text)` — 그 run 에서 `read_macro` 로
    읽지 않았으면 거절. **`old_text` 는 줄 경계를 안 보는 평범한 부분 문자열이고,
    초안 안에서 등장 횟수가 정확히 1 이 아니면 거절한다**(0 이면 못 찾았다고, 2 이상이면
    어느 것인지 모른다고 말한다). 등록된 것만 있으면 초안으로 복사한 뒤 고친다.
  - `read_macro(step, thought, name)` — 초안이 있으면 초안, 없으면 등록된 것의 원문.
  - `register_macro(step, thought, name, screens=[])` — 초안을 검사해 통과하면 부를 수
    있게 한다. 같은 이름의 등록 행은 제자리에서 갱신하고 기존 `screen` 관계를 남긴다.
    `screens` 의 값 모양은 `ARTEL-919` 가 정하므로 해석하지 않고 그대로 모아 둔다.
  - `run_macro(step, thought, name, args)` — 등록된 것만. 선언 타입 검사와 좌표·`#` id
    문자열 검사를 첫 statement 전에 끝낸다.
  - 설명은 `app/agents/qa/macro/descriptions.py` 가 든다 — `screen.py` 와
    `capability.py` 의 선례다. 받을 수 있는 것의 목록(tool 열넷·reader 여덟·연산자
    여섯·타입 다섯·상한 둘)은 `grammar.py` 의 데이터에서 조립한다. 손으로 다시 적으면
    모델이 읽는 글이 parser 와 어긋난다. `write_macro` 의 설명에 number 의 `==` 권고와
    `<`·`>` 를 `require` 둘로 쓰는 관용구를 싣는다.

- [ ] **Step 6: Tests**
  - `tests/test_qa_macro_parser.py` — 허용 사례와 거절 사례를 각각 표로. 이슈의
    `## Validation Notes` 를 한 줄씩 옮긴다. 상한(깊이 3/4, statement 128/129),
    이름 묶기, 타입 검사, target 자리, `require`·`flag`·`ask_verdict` 의 인자 모양.
  - `tests/test_qa_macro_operators.py` — **마흔두 칸을 한 군데서 든다.** 손으로 쓴
    기대 표 하나(`_EXPECTED_MATRIX`, 키 42개임을 먼저 단정)를 두고, 그 표로 parser 의
    저장 시점 거절(ARTEL-917)과 binding 의 평가 시점 거절(ARTEL-920)을 **둘 다**
    돌린다. 표를 두 벌 쓰면 어긋나고, `grammar.py` 의 표로 parametrize 하면 표가
    자기 자신을 검사한다.
  - `tests/test_qa_macro_model.py` — round-trip, 선언 타입 보존, 진입점·helper 구분,
    정의를 고치는 유일한 길이 원본 텍스트를 다시 파싱하는 것이라는 것(모델이 frozen
    이고, tool 도 runner 도 `model_validate` 로 정의를 만들지 않는다).
  - `tests/test_qa_macro_binding.py` — 네 코드 갈림(유일·둘 이상·없음·사라짐),
    `under=` prefix, `observable` 만 `SceneMemory` 를 읽는 것, `unread` 모양,
    `id` 가 `None` 인 두 기록의 `==`.
  - `tests/test_qa_macro_runner.py` — 9-statement 예시에서 6번째 `require` 실패 시
    `applied` 4-5, `pending` 7-9, `skipped` 빈 목록. helper 안의 실패와 호출 사슬,
    거짓 `if` 몸통이 `skipped` 로 가고 `pending` 에는 안 드는 것, 참인 `if` 몸통이
    `applied` 로 가는 것, `flags`/`verdict_requests` 와 `observed`,
    `ast.unparse` 결과가 `ACTION` frame 에 남는 것(따옴표가 바뀔 수 있음을 기대값에
    반영), `enter_text` 의 `id is None`.
  - `tests/test_qa_macro_tools.py` — write → register → run end-to-end, 그리고 이
    층에만 있는 규칙을 하나씩: `read_macro` 없이 `edit_macro` 거절, `old_text` 가
    0번/2번 나올 때 거절, 등록된 것만 있을 때 초안으로 복사되고 등록 행은 그대로,
    고치는 중에도 `run_macro` 가 등록된 것을 부르는 것, `register_macro` 가 제자리에서
    갱신하며 기존 `screen` 관계를 남기는 것, `screens` 가 기존 관계에 더해지는 것,
    서 있는 `screen` 을 모르면 관계가 빈 채로 등록되는 것, 좌표/`#` id 호출 인자가
    게임에 아무것도 보내기 전에 `ACTION_REJECTED` 로 거절되는 것, 깨진 문법과 모르는
    tool 이름이 `MACRO_NODE_REJECTED` 인 것, 그리고 tool 다섯이 `build_tools` 에서
    action tool 뒤 · reporting 앞에 서는 것.
  - `tests/test_qa_macro_grammar.py` — `grammar.py` 의 tool 열넷이 실제
    `build_tools` 의 tool 이름이고 macro 쪽 파라미터 이름이 그 tool 의 `args_schema` 에
    실제로 있는 이름인지. 손으로 옮긴 표가 늙는 것을 이 테스트가 잡는다. 그리고
    `write_macro` 설명에 tool 열넷과 reader 여덟의 이름이 전부 나오는지.
  - `tests/test_qa_arch.py` — pin 두 개를 고친다. tool 이름 다섯은
    `_EXPECTED_DEFAULT_TOOL_NAMES` 의 `reset_game` 다음 · `wait_for_operator` 앞에
    넣고, 왜 label 은 안 올렸는지(`ARTEL-926`)를 주석으로 적는다.

- [ ] **Step 7: Rollout / Rollback** — flag 없다. tool 다섯이 목록에 드는 것이 곧
  노출이고, revert 는 commit 을 되돌리는 것뿐이다.

## Validation

- **Commands to run:**
  - `LANGSMITH_TRACING=false python -m pytest tests/test_qa_macro_parser.py tests/test_qa_macro_operators.py tests/test_qa_macro_model.py tests/test_qa_macro_binding.py tests/test_qa_macro_runner.py tests/test_qa_macro_tools.py tests/test_qa_macro_grammar.py -q`
  - `LANGSMITH_TRACING=false python -m pytest tests/test_qa_arch.py tests/test_qa_tools.py -q`
  - `LANGSMITH_TRACING=false python -m pytest -q` (전체. `tests/test_config.py` 두 개는
    `~/.zshenv` export 때문에 원래 깨져 있다)
- **Expected output:** 새 파일의 테스트 전부 통과. `test_qa_arch.py` 는 pin 을 고친 뒤
  통과. 기존 실패는 `test_config.py` 두 개뿐.

## Risks & Rollback

- **Risks:**
  - `arch_fingerprint` 가 움직이는데 `QA_ARCH_LABEL` 이 안 올라간다. `ARTEL-926` 까지
    기본 런의 tool 목록이 label `v5-pointer-target` 아래에 두 가지로 쌓인다.
  - orchestration 저장이 없어 등록된 macro 도 런이 끝나면 사라진다. 자리는
    `MacroBook.register` 하나로 좁혀 두되, 그 위에서 돌아 본 적은 없다.
  - `grammar.py` 의 tool 표가 `action_tools.py` 의 조립과 따로 산다. 두 벌이고,
    `tests/test_qa_macro_grammar.py` 가 유일한 연결이다.
  - parser 의 거절 표가 넓어 저자(모델)를 막는 자리가 많다. 거절 문장이 받을 수 있는
    것을 이름으로 대는 것이 유일한 완화다.
  - QA 런으로 재 본 수가 없다. tool 목록이 바뀌었으므로 성적은 `ARTEL-926` 이
    label 을 올릴 때 함께 재는 것이 맞다.
- **Rollback steps:** `git revert` 로 commit 을 되돌린다. tool 다섯이 목록에서 빠지면
  macro 를 부를 길이 없어지고, 저장된 것이 런의 상태에만 살아 남는 것이 없다.

## Rejected feedback

- **`ctx.run` 에 구조화된 반환을 새로 만들라**(medium #3). 안 한다. 이슈가 `applied` 를
  "이미 나간 action statement" 로 정의하므로 runner 에 필요한 것은 보냈다는 사실이고,
  개별 action 의 성공 여부가 아니다. 그리고 `ToolContext.run` 을 고치는 것은 기존
  소비자 열여섯 개를 건드리는 일이다.
- **`action_tools.py` 에서 request builder 를 뽑으라**(medium #1 의 첫 안). 안 한다.
  기존 것을 고치지 말고 옆에 더하라는 제약과 부딪힌다. 대신 `grammar.py` 의 표가
  `method` 와 인자 순서를 들고, `tests/test_qa_macro_grammar.py` 가 둘이 어긋나지
  않는지를 본다.
- **모델이 frozen 인지 보는 테스트를 지우라**(medium #10). 안 지운다. `ARTEL-918` 이
  "정의를 고치는 유일한 길은 원본 텍스트를 다시 파싱하는 것임을 단위 테스트로
  확인한다" 를 AC 로 적는다. 다만 pydantic 을 검사하는 데서 그치지 않게, tool 과
  runner 가 `model_validate` 로 정의를 만들지 않는다는 것까지 본다.

## Settled during implementation

- **`find` 의 keyword 값으로 맨이름을 받을 때 그 이름의 선언 타입.** `ARTEL-923` 의
  예시가 `find(label=card_a)` 인데 `ARTEL-917` 의 예시에서 `card_a: object` 다. `label`
  은 `PulseObject.text` 를 맞추는 문자열이므로 `string` 선언만 받고 `object` 는
  거절한다. 두 이슈가 어긋난 자리이고 PR 본문에 적는다.
- **조건이 비교 없이 reader 호출 하나뿐일 때.** 계획은 여덟 전부를 허용하고 Python 의
  truthiness 로 평가하기로 했었다. **그것을 뒤집었다.** 그 모양이면 빈 문자열이 거짓,
  아무 사전이 참이 되어 조용히 틀리는데, 그것이 이 이슈가 거절로 막겠다고 적은 바로 그
  경우다. 모양을 저장 시점에 아는 다섯은 parser 가 거절하고(`actionable`·`exists`·
  `absent` 만 통과), 모르는 셋은 값이 도착한 뒤 `binding.evaluate` 가 같은 거절을 한다.
  이슈가 적지 않은 거절을 더한 것이므로 PR 본문에 적는다.
- **눌렀으면 풀어야 하는 tool 의 카운터.** 이슈 다섯 중 어디에도 없고, 진행 중에 받은
  지시로 더했다. 경로마다 따로 세고 어느 경로에서든 끝에 0 이 아니면 거절한다. 경로를
  펼치는 대신 `if` 의 두 가지가 다른 값을 남기면 거기서 거절한다 — 128 statement 면
  경로가 천문학적인 수가 되고, 두 가지가 다르면 그런 경로가 반드시 하나 생긴다.
- **`run_macro` 의 인자 이름이 `args` 가 아니라 `arguments` 다.** langchain 이 함수
  서명에서 argument schema 를 떠낼 때 pydantic 이 `args` 를 내부 이름과 부딪히는 것으로
  보고 `v__args` 로 바꾸므로 호출이 통째로 깨진다.
- **호출 그래프를 걷는 자리를 하나로 뒀다.** `_CallGraph` 하나가 네 검사(재귀·깊이·
  statement 총수·카운터)의 질문을 받고 답을 전부 memo 한다. 네 검사가 각자 그래프를
  세우고 memo 를 손으로 달았더니 `depth` 하나가 memo 를 빠뜨려 2^n 이 됐다 — helper
  스물넷(99줄)에서 5초, 서른에서 5분이었고 그것이 `write_macro` tool 호출 안이다.
- **`grammar.py` 의 tool 표를 `action_tools.py` 와 맞추는 테스트.** 계획의 Risks 가
  그것을 유일한 연결로 적었는데 첫 구현에 없었다. `tests/test_qa_macro_grammar.py` 가
  열넷 각각에 대해 같은 조작을 두 길로 보내고 나간 `method` 순서와 `params` 순서를
  견준다. `params` 순서를 일부러 뒤집어 보고 실제로 걸리는 것을 확인했다.
