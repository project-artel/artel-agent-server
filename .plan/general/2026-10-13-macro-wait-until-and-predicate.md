# 2026-10-13 — macro 에 wait_until statement 와 bool 을 돌려주는 predicate def 추가

- Date: 2026-10-13
- Jira: ARTEL-988
- Status: Reviewed (fast · medium · heavy 반영)
- Base: ARTEL-984 (PR #215) 위에 stack 한다

## Goal

1. `wait_until(<조건>, <초>, <remedy>)` statement. 조건이 참이 될 때까지 새 `pulse` 를 기다리며
   다시 평가하고, 시간이 다 되면 `WAIT_TIMEOUT` 으로 멈춘다.
2. `-> bool` 인 predicate `def`. 조건 자리(`require`, `if`, `while`, `wait_until`, predicate 의
   `return`)에서 부른다. `or` 가 필요한 조건을 이것으로 적는다.

## Non-goals

- orchestration-server 변경. `MACRO_REGISTER` 의 `definition` 은 passthrough 이고 저쪽이 기대는
  것은 `require` node 의 `kind` 와 `remedy` 둘뿐이다(`MacroDefinitionService.kt:165`).
- `and` · `or` · `not` 연산자. 계속 거절한다.
- predicate 를 비교의 피연산자로 쓰는 것(`p(x) == False`). 조건 하나가 predicate 호출 하나다.
- 대기 시간 상한 조정. 30초와 60초는 첫 값이다.

## Context / Constraints

- 2026-09-18 계획 문서와 다르게 구현에는 `for` · `while` 이 있다(ARTEL-948). 그래서 macro 한 번의
  대기 시간 합계는 저장할 때 계산할 수 없고, `MAX_LOOP_PASSES` · `MAX_EXECUTED_STATEMENTS` 처럼
  실행 중에 센다.
- `while` 은 pass 사이에 `pulse` 를 기다리지 않는다(`runner.py` `_passes`). action 없는 `while` 은
  같은 `PulseMemory` 를 50번 읽고 `LOOP_LIMIT` 이다. `wait_until` 이 필요한 이유다.
- runner 는 `ToolContext` 를 모른다. 바깥 일은 `MacroHost` protocol 로만 한다. 테스트는 가짜 host 로
  돈다. 그래서 흐른 시간도 host 가 재서 돌려준다 — runner 가 시계를 직접 읽으면 가짜 host 테스트가
  실제로 잠든다.
- 저장된 macro 는 JSON 이 아니라 `source` 를 다시 파싱해서 쓴다(`macro_tools.py:297-302`,
  `macro_definition_from_source`). `MacroReadResultPayload.definition` 은 읽지 않는다.
- SDK 는 화면이 그대로면 `pulse` 를 안 보낸다. 움직임이 없는 화면에서 거짓인 `wait_until` 은 매번
  시간을 다 쓴다. 정상 동작이고 payload 의 `readings` 가 그것을 보여 준다.

## Contract (모든 track 이 이것에 맞춘다)

### `model.py` (Step 1, 완료)

- 인자 묶음(`MacroArgument`)을 조건 묶음 위로 옮겼다. 순환 참조는 없다.
- `MacroPredicateCondition(kind="predicate", callee, arguments)` 를 `MacroCondition` 에 더했다.
- `MacroWaitUntilStatement(kind="wait_until", condition, seconds: float, remedy: str)`.
- `MacroReturnStatement(kind="return", condition | None, value: bool | None)`. `model_validator` 가
  정확히 하나만 차게 한다.
- `MacroFunction.returns: Literal["none", "bool"] = "none"`. 기본값으로 예전 JSON 이 그대로 읽힌다.
- predicate 는 `MacroDefinition.helpers` 에 함께 든다.

### `grammar.py` · `errors.py` (Step 1, 완료)

- `WAIT_UNTIL = "wait_until"`, `MAX_WAIT_PER_STATEMENT_SECONDS = 30`,
  `MAX_WAIT_PER_CALL_SECONDS = 60`.
- `WAIT_TIMEOUT` 을 더하고 `RUNTIME_CODES` 에 넣었다.

### `runner.py` 의 host 계약

- `MacroHost.wait_for_pulse(timeout: float) -> MacroPulseWait`.
  `MacroPulseWait(arrived: bool, waited: float, operator_messages: tuple[str, ...] = ())`.
  - 새 `pulse` 는 **부른 순간의 `scene.pulse.readings` 보다 개수가 늘어난 것**이다. 부르기 전에 온
    것은 세지 않는다.
  - operator 가 말하면 바로 돌아오고, host 는 말을 **비우지 않고 복사해서** 싣는다. runner 는 첫
    말에서 `_operator_spoke` 로 바로 멈추고, `_drive` 가 지금처럼 `drain_operator_messages()` 로
    비워 답 끝에 "applies from now on" 묶음으로 붙인다. 그래서 channel 에 비우지 않고 읽는 공개
    method `pending_operator_messages() -> tuple[str, ...]` 를 둔다.
  - `waited` 는 host 가 잰 실제 초다.
  - 런 취소(`QaCancelled`)는 예외로 그대로 올라간다.
- 60초 상한은 **tool 호출 하나**에 건다. `run_macro` 한 번, 또는 `checkpoint` 뒤 `resume_macro`
  한 번이 각각 tool 호출 하나이므로, runner 는 `_checkpoint` 에서 agent 가 이어 가기로 하면 쓴 양
  (`self.waited_this_call`)을 0 으로 돌린다. `MacroRunResult.waited: float` 는 macro 전체의 합계로
  보고에만 쓴다. 근거는 `grammar.py` 의 주석 그대로다 — operator 가 기다리는 것은 tool 호출 하나다.
- 성공한 `wait_until` 은 `MacroRunResult.waits: list[str]` 에 한 줄을 남기고, `_render` 가 그것을
  "Waits:" 아래에 그린다: `` `<source>` was true after 3.2s (4 pulses) ``, 바로 참이었으면
  `` `<source>` was already true ``. "What the game said:" 아래의 `outcomes` 에 섞지 않는다 — 그
  줄은 게임이 한 말이 아니다.

### `WAIT_TIMEOUT` payload

`expected`(조건 원문), `observed`(마지막 평가에서 읽은 값, 기존과 같은 dict), `remedy`,
`waited_seconds`(그 statement 가 기다린 초, 소수 첫째 자리), `readings`(도착한 `pulse` 수),
`evaluations`(평가 횟수), `limit`(`"statement"` 또는 `"call"`). `reason` 문장은
`` `<source>` was still false after {waited}s ({readings} pulses arrived). {remedy} `` 이고,
`limit == "call"` 이면 tool 호출 하나의 60초를 다 썼다고 덧붙인다. **그 statement 가 0초보다
오래 기다렸고** `readings == 0` 일 때만 "no pulse arrived — the screen did not change at all" 을
덧붙인다. 상한을 이미 다 써서 기다리지 못하고 멈춘 경우에는 그 말이 거짓이다.

### predicate 실행 규칙

- `_Runner._evaluate(frame, condition, observed)` 가 조건을 평가하는 **유일한 입구**다.
  `_require`, `_branch`, `_passes`(`while`), `_wait_until`, predicate 안의 `if` · `return` 이 전부
  이것을 부른다. 지금 `binding.evaluate` 를 직접 부르는 세 자리를 바꾼다.
- predicate 는 자기 scope 와 번호를 위해 `_Frame` 을 하나 만들지만 `self.frames` 에 **올리지
  않는다.** 올리면 `pending()` 이 predicate 의 줄을 "Did NOT reach the game" 으로 그린다. predicate
  안의 `if` 는 `frame.skipped` · `result.skipped` 에 아무것도 안 더하고 `self.observed` 에도 아무것도
  안 올린다. 읽은 값은 부른 쪽의 `observed` dict 에 들어간다. predicate 안에서 난 `MacroFailure` 는
  payload 에 `predicate: "<fn> step N"` 을 붙여 다시 올린다. 실패 자리는 그 predicate 를 부른
  statement 다.
- `_passes` 에서 `while` 조건 평가가 예외를 내면 `frame.at = loop_number` 로 맞춘 뒤 올린다. 지금은
  두 번째 회부터 `frame.at` 이 몸통의 마지막 줄을 가리킨다(`runner.py:609`).
- 끝난 `return` 은 `observed["<fn> returned at"] = "step N"` 으로 남긴다.
- predicate 의 statement 는 `MAX_EXECUTED_STATEMENTS` 에 세지 않는다. 반복이 없어 반드시 끝나고,
  길이는 저장 때 `MAX_STATEMENTS` 로 센다. `wait_until` 이 같은 predicate 를 수십 번 평가하므로 실행
  수에 넣으면 1000 을 기다림만으로 쓴다.
- predicate 가 predicate 를 부를 수 있다. 재귀 금지와 `MAX_CALL_DEPTH` 는 helper 와 같이 저장 때
  판정하고, 실행 중 깊이 검사는 따로 두지 않는다 — 정의는 parser 를 거친 것만 runner 에 온다.

### `SELECTOR_NOT_FOUND` 규칙

`wait_until` 이 조건을 평가하는 동안 난 `SELECTOR_NOT_FOUND` 는 그 평가 전체를 거짓으로 본다.
그 조건이 부른 predicate 안의 `find` 대입에서 난 것도 같다. 사유 문장은 `observed["not found"]` 에
남긴다. `require` · `if` · `while` 이 부른 predicate 안에서는 지금처럼 멈춘다. 있을 수도 없을 수도
있는 객체로 `or` 를 적을 때는 `exists(selector(...))` 를 쓴다 — 그것은 이미 예외 없이 거짓을 낸다.
`STALE_BINDING`, `SELECTOR_AMBIGUOUS`, `COMPARISON_REJECTED` 는 `wait_until` 안에서도 멈춘다.

## Approach (Checklist)

- [x] **Step 0: Recon**
- [x] **Step 1: Contract** — `model.py`, `grammar.py`, `errors.py`. 모델 테스트 44개 통과.
- [ ] **Step 2a: parser track** — 쓰는 파일: `parser.py`, `tests/test_qa_macro_parser.py`,
  `tests/test_qa_macro_model.py`.
  - `_parameters`: `-> None` 과 `-> bool` 을 받는다. `_Signature` 가 반환을 든다.
    `macro_definition_from_source` 가 진입점이 `-> bool` 이면 거절한다("the entry point acts; a
    `-> bool` def is a predicate that a condition calls").
  - predicate 몸통은 대입 · `if` · `return` 셋만 받는다. 그 밖은 셋을 이름 대고 거절한다.
  - `return`: predicate 안에서만. 값은 `True` · `False` 리터럴(`value`)이거나 `_condition`.
    모든 경로가 `return` 으로 끝나야 한다(`if` 는 body 와 orelse 둘 다 끝나야 끝난 것, `elif` 사슬
    포함). `return` 뒤의 statement 는 거절. `-> None` 안의 `return` 은 지금처럼 거절하되 문장에
    predicate 를 이름 댄다.
  - `_condition`: `-> bool` 인 `def` 의 호출은 `MacroPredicateCondition`. 인자 검사는
    `_arguments` + `_helper_parameters` 로 helper 호출과 같다. `-> None` 인 `def` 를 조건에 쓰면 거절.
  - `_call_statement`: `wait_until` 을 받는다. predicate 를 statement 로 부르면 거절.
  - `wait_until`: positional 인자 셋, keyword 거절. 둘째는 bool 이 아닌 int · float 리터럴,
    0 초과 30 이하. 셋째는 비어 있지 않은 문자열 리터럴.
  - predicate 몸통 제한은 `_statement` 에서 건다. 그래야 중첩된 `if` 몸통 안에도 걸린다.
  - `_STATEMENT_KINDS`(`parser.py:152`)에 `wait_until` 을 더한다.
  - `_called_helpers`: statement 만이 아니라 조건(`require`, `if`, `while`, `wait_until`, `return`)
    안의 predicate 호출도 모은다. `reject_recursion` 과 `depth_of` 가 이것으로 predicate 를 센다.
  - `_CallGraph._count`: `wait_until` 과 `return` 은 1. 조건이 predicate 를 부르는 statement 는
    그 predicate 의 `statements_in` 을 더한다.
  - `_tally_body` 와 `reject_unpaired`: predicate 에는 action 이 없으므로 그대로 둔다.
  - `reject_checkpoint_while_holding` 의 `walk`: `MacroWaitUntilStatement` 에서 `frozen game time`
    의 level 이 0 이 아니면 거절한다. 눌린 키와 마우스 버튼은 허용한다.
- [ ] **Step 2b: runner track** — 쓰는 파일: `runner.py`, `app/agents/qa/tools/macro_tools.py`,
  `app/qa/channel.py`, `tests/test_qa_macro_runner.py`, `tests/test_qa_macro_tools.py`,
  `tests/test_qa_channel.py`.
  - 2a 가 끝나기 전에는 parser 가 새 문법을 거절하므로, runner 테스트는 test 파일 안의 작은 helper
    로 `MacroDefinition` · `MacroFunction` 을 직접 만든다. source 텍스트로 쓰는 테스트는 Step 4 에서
    몇 개 더한다.
  - `_statement` 에 `MacroWaitUntilStatement` 분기. `MacroReturnStatement` 가 오면 assert —
    parser 가 predicate 밖의 `return` 을 거절한다. 그 밖의 kind 가 `_action` 으로 떨어지지 않게 한다.
  - `_evaluate`, `_predicate`: 위 규칙대로.
  - `_wait_until`: 처음 한 번 바로 평가. 거짓이면
    `limit = min(seconds - waited_here, MAX_WAIT_PER_CALL_SECONDS - self.waited_this_call)`.
    `MacroPulseWait.waited` 는 `waited_here`, `self.waited_this_call`, `result.waited` 셋에 모두 더한다. `limit <= 0`
    이면 `WAIT_TIMEOUT`(`limit` 칸은 어느 상한이 먼저 닿았는지). 아니면 `host.wait_for_pulse(limit)`,
    operator 말이 있으면 `_operator_spoke` 로 멈춘다. `arrived` 가 거짓이면 시간이 다 된 것이므로
    다시 계산하지 않고 바로 `WAIT_TIMEOUT` 이다. 도착했으면 `readings += 1` 하고 다시 평가한다.
  - `channel.py`: `_await_reading(after, timeout, frame=None)` 에 `interrupt: asyncio.Event | None`
    을 더한다. loop 머리에서 `interrupt.is_set()` 이면 돌아오고, 기다릴 때는 두 이벤트를 함께
    기다린 뒤 남은 waiter task 를 cancel 한다. 공개 method `wait_for_reading(timeout) -> bool` 은
    첫 `await` 전에 `readings` 를 잡고, `_operator_arrived` 를 interrupt 로 넘기고, 기다리기 전과
    깨어난 뒤에 `_raise_if_cancelled()` 를 부른다 — `on_cancel` 은 말 없이 `_operator_arrived` 만
    켜므로, 안 부르면 STOP 뒤에도 남은 시간을 다 기다린다. `on_pulse` · `on_chat` 이 같은 event
    loop 의 동기 callback 이라 `clear()` 위치에 경쟁이 없다. 대기 loop 를 새로 쓰지 않는다.
  - `pending_operator_messages()`: 비우지 않고 복사해서 낸다.
  - `macro_tools.py`: `_Host.wait_for_pulse` 가 `loop.time()` 으로 시간을 재고 operator 말을
    `pending_operator_messages()` 로 읽는다(비우지 않는다). `_render` 가 실패 payload 의
    `waited_seconds`, `readings`, `evaluations`, `predicate` 와 "Waits:" 목록을 그린다.
- [ ] **Step 3: prompt track (2a · 2b 뒤에 순서대로)** — 쓰는 파일: `app/prompts/qa_run/v21/`,
  `app/prompts/prompts-lock.json`, `app/agents/qa/arch.py`, `app/agents/qa/macro/reference.py`,
  `tests/test_qa_arch.py`, `tests/test_qa_prompt_version.py`, `tests/test_qa_phase_cycle.py`.
  - `v20` 을 `v21` 로 복사. `skill_macro.md`, `tool_write_macro.md`, `tool_run_macro.md`(필요하면
    `macro_memory_directive.md`)에 `wait_until`, predicate, `WAIT_TIMEOUT` 을 적는다. v20 은 안 고친다.
  - `python -m app.prompts.lock --write`.
  - `tests/test_qa_prompt_version.py`: 기본 version 을 v21 로, "v21 이 v20 의 나머지를 그대로 든다"
    테스트를 v20 것을 본떠 더한다. `tests/test_qa_phase_cycle.py:108` 의 version 목록에
    `("v21", "on")` 을 더한다. `resolve_version` 이 가장 새 version 을 고르므로 설정은 안 바꾼다.
  - `QA_ARCH_LABEL = "v22-macro-wait-until"` 과 그 위 문단. `arch_fingerprint` 는 안 움직인다(tool
    목록과 인자 schema 가 그대로다). 그래서 `tests/test_qa_arch.py` 는 label pin 만 바뀐다. bump 하는
    이유: 같은 tool 이지만 macro 가 할 수 있는 일이 바뀌어, 두 런을 한 bucket 에 두면 기다릴 수 있는
    agent 와 없는 agent 가 섞인다.
  - `reference.py` 와 `macro_tools.py` 머리의 `v20` 경로를 `v21` 로.
- [ ] **Step 4: 통합 테스트** — macro 테스트 전체, 그 다음 전체 pytest.
- [ ] **Step 5: 측정** — `qa-run-local` 로 변경 전(v20 prompt, v20 label)과 변경 후(v21 prompt,
  v21 label). 이 짝은 `prompt_version` 도 다르다 — 문법 설명이 prompt 에 있어서 피할 수 없다. PR
  에 그렇게 적는다.

## Validation

- **Commands:** `LANGSMITH_TRACING=false <venv>/bin/python -m pytest -q`
- **기준선:** macro 관련 633개 통과(fe13553).
- 새 테스트:
  - parser: predicate 받기 · 거절 각 경우, `target` 인자를 받는 predicate, `elif` 사슬의 종료 판정,
    `return` 뒤 statement 거절, 진입점 `-> bool` 거절, `wait_until` 의 `seconds` 가 `True` · `0` ·
    `31` · 이름일 때 거절, keyword 거절, 재귀 · 깊이에 predicate 포함, 시간 멈춘 채 기다리는 경로
    거절, 키를 누른 채 기다리는 경로 허용, 모델 JSON 왕복과 `returns` 기본값.
  - runner: 바로 참, 몇 번 기다린 뒤 참, statement timeout, call 합계 소진, 합계가 이미 0 일 때
    (그때 "screen did not change" 문장이 없는 것), `checkpoint` 뒤 이어 돌면 상한이 다시 차는 것,
    `while` 조건의 predicate 가 실패할 때 `stopped_at` 이 `while` 줄인 것,
    `SELECTOR_NOT_FOUND` 를 거짓으로(직접 조건과 predicate 안 `find` 둘 다), `SELECTOR_AMBIGUOUS` 는
    멈춤, operator 끼어듦, `QaCancelled` 통과, predicate 의 `or`, `while` 조건의 predicate,
    predicate 안 실패의 `predicate` payload.
  - tools: `_render` 의 `WAIT_TIMEOUT` 줄과 "Waits:" 목록, `_Host.wait_for_pulse` 가 operator 말을
    비우지 않고 `_drive` 가 그것을 답 끝에 붙이는 것.
  - channel: `wait_for_reading` 이 새 `pulse`, operator 말, 취소 각각에서 돌아오고, 취소면
    `QaCancelled` 를 낸다. 부르기 전에 온 `pulse` 는 세지 않는다.

## Risks & Rollback

- **Risks:**
  - `pulse` 가 안 오는 화면에서는 거짓인 `wait_until` 이 매번 시간을 다 쓴다. `readings: 0` 으로
    드러난다.
  - tool 호출 하나가 최대 60초 더 걸린다. 런 deadline 은 그대로 이것을 포함한다.
- **Rollback:** `git revert`. 되돌린 parser 는 `wait_until` 이나 `-> bool` 이 든 저장된 `source` 를
  다시 파싱할 때 `MACRO_NODE_REJECTED` 로 거절하고, agent 에게 거절 문장으로 보인다. 그 macro 만 다시
  쓰기 전까지 못 쓰고 런은 계속된다.

## Rejected feedback

- `MacroReturnStatement` 를 bool 리터럴 조건 하나로 합치기: 조건 문법에 리터럴 조건을 열면
  `require(True, ...)` 를 따로 막아야 한다. 두 칸과 validator 가 더 좁다.
- `QA_ARCH_LABEL` bump 를 생략하기: AGENTS.md 의 네 조건에 직접 들지는 않지만, Jira 수용 조건이고
  macro 가 할 수 있는 일이 바뀐 런을 같은 label 로 묶지 않으려는 것이다.
- `channel._await_reading` 을 host 가 직접 부르기: 다른 module 에서 private method 를 부르게 된다.
  대기 loop 는 재사용하고 공개 입구만 하나 둔다.
- 성공한 `wait_until` 을 `outcomes` 에 넣기(medium 리뷰): heavy 리뷰가 그것이 "What the game said:"
  아래에 그려진다고 지적해서 `waits` 목록을 따로 둔다.
- host 가 operator 말을 drain 하기(fast 리뷰): heavy 리뷰가 답 끝의 "applies from now on" 묶음이
  사라진다고 지적해서 비우지 않고 읽는 쪽으로 바꿨다.

## Open Questions

- 없음. `find` 규칙은 사용자가 확정했다(2026-10-13).
