# 2026-10-14 — QA agent 가 게임이 등록한 cheat 와 behavior 를 호출하는 tool 추가

- Date: 2026-10-14
- Jira: ARTEL-1054 (Story ARTEL-1051, Epic ARTEL-1050)
- Status: Draft
- Contract: workspace `.plan/general/2026-10-14-game-functions-wire-contract.md` (binding)

## Goal

agent-server 쪽 몫을 계약대로 구현한다.

- `QaArchSpec.cheats`, `QaArchSpec.behaviors` (`"off"` 기본). 켜졌을 때만 tool 이 붙는다.
  - `run_cheat`, `start_behavior`, `stop_behavior`, `extend_behavior`
- session open 의 `context.cheats` / `context.behaviors` 를 첫 user message 에 싣는다.
- test case 의 `functions` 로 호출을 거절한다. 거절문은 그 test case 가 허용하는 이름을 댄다.
- `target` 일부만 적으면 `pulse` memory 로 전체 selector 를 찾는다. 0개나 여러 개면 아무것도
  보내지 않고 tool 결과로 실패한다.
- `PULSE.functions` / `PULSE.behaviors` 를 `PulseMemory` 에 두고 `pulse` view 에 `Cheats now /
  not now`, `Behaviors now / not now`, 실행 중인 behavior 한 줄씩을 그린다.
- `BEHAVIOR_ENDED` 를 받아 다음 tool 결과에 붙인다.
- `report_step` 의 `STATUS` 에 `used_cheats` 와 `active_behaviors` 를 agent-server 가 채운다.
  tool 결과에 아직 도는 behavior 를 적는다.
- 다음 test case 가 허용하지 않는 behavior 는 그 case 의 첫 step 전에
  `stop_behavior(reason="not_allowed")` 로 멈춘다.
- prompt `qa_run/v24`: v23 을 그대로 옮기고 tool 설명 넷, skill 둘(`cheats`, `behaviors`)을 더한다.
- `QA_ARCH_LABEL` 을 올린다.

## Non-goals

- scenario agent 가 test case `functions` 를 채우는 일. 아래 Context 참고.
- 실제 QA run (`qa-run-local`). SDK 와 orchestration 변경이 아직 merge 되지 않았다.
- 사람이 cheat 를 직접 실행하는 경로, release build.

## Context / Constraints

- 설계 plan 두 개(`2026-10-13-game-registered-cheats-for-the-qa-agent.md`,
  `...-behaviors-the-agent-runs-in-background.md`)는 디스크에도 git 이력에도 없다. 계약 파일과
  Jira ARTEL-1050/1054 본문, 작업 지시의 규칙만 근거로 쓴다.
- scenario agent (`app/agents/scenario/`)는 scenario step (`AuthoredStep`) 만 쓰고 test case 본문을
  쓰지 않는다. test case 는 orchestration 이 `test_case_list` 로 내려 주는 읽기 전용 값이고,
  `ScenarioAgentRequest` 에는 build 의 function catalog 가 없다. 그래서 이 작업에서 뺀다.
- tool 목록의 순서가 계약이다(`tools/__init__.py`). 새 tool 은 맨 뒤에 붙여 축이 꺼진 run 의
  목록이 한 글자도 안 바뀌게 한다.
- `qa_run/v23` 은 develop 이 macro 호출 깊이 상한(ARTEL-1056)으로 먼저 release 해서 lock 되어 있다. 새 version `v24` 를 만든다.
- label: 모든 branch 에서 가장 높은 것이 `v26-macro-result-fold` 다. `v27-game-functions` 로 한다.

## Approach (Checklist)

- [ ] **Step 0: Recon** — 끝남. 읽은 파일: `arch.py`, `tools/__init__.py`, `tool_context.py`,
      `action_tools.py`, `reporting_tools.py`, `state.py`, `phase.py`, `envelope.py`, `channel.py`,
      `pulse.py`, `schemas.py`, `service.py`, `api/qa_sessions.py`, `runner.py`, `run_config.py`,
      prompt loader/lock.
- [ ] **Step 1: 모델** — `app/qa/game_functions.py` 새 파일.
  - `GameFunctionParam`, `GameFunctionEntry` (계약 1절), `CaseFunctions` (`cheats`, `behaviors` 가 각각
    `"all" | "none" | list[str]`, 기본 `"all"`), `FunctionPulseEntry`, `PulseFunctions`,
    `RunningBehavior`, `BehaviorEndedPayload`.
  - 순수 함수: `allowed_names(run_names, permission)`, `resolve_target(partial, candidates)`,
    `render_catalog(cheats, behaviors)`, `render_function_lines(...)`, `render_behavior_ended(...)`.
- [ ] **Step 2: wire**
  - `envelope.py`: `MessageType.BEHAVIOR_ENDED`; `ActionResultItem.returnValue` 를 `Any` 로 넓힌다
    (`capture_from_action_result` 는 dict 일 때만 읽게 고친다); `StatusPayload.used_cheats`,
    `active_behaviors` (기본 `None`, `None` 이면 frame 에서 key 를 뺀다 — 축이 꺼진 run 의
    `STATUS` frame 이 byte 단위로 그대로다).
  - `schemas.py`: `QaCaseRef.functions: CaseFunctions | None`; `QaSessionRecord.cheats`,
    `.behaviors`.
  - `api/qa_sessions.py`: `QaContext.cheats`, `.behaviors` → `service.open` → record.
  - `pulse.py`: `PulseReading.functions`, `.behaviors`; `PulseMemory.functions` (key
    `kind|name|target`), `.behaviors`, `.function_view` (현재 test case 가 허용하는 이름, 축이 꺼져
    있으면 `None`). `apply` 가 합치고(`whole` 이면 바꾸고, `removed` 면 지운다) `render` 가 head
    줄 다음에 줄을 그린다. `function_view` 가 `None` 이면 아무 줄도 없다 — 기본 run 의 view 가
    그대로다.
  - `channel.py`: `on_behavior_ended` 가 `pulse.behaviors` 에서 빼고 큐에 쌓는다.
    `drain_behavior_endings`.
  - `service.py`: `deliver` 에 `BEHAVIOR_ENDED`; `run_with_deadline` 에 catalog 를 넘긴다.
- [ ] **Step 3: arch** — `cheats`, `behaviors` (`Literal["off","on"] = "off"`),
      `max_cheats_per_run`, `max_behavior_calls_per_run` (기본 1,000,000, 다른 allowance 와 같은
      ceiling). `tool_call_limit` 은 축이 켜졌을 때만 더한다. `withheld_skills` 에 `cheats`,
      `behaviors` 추가. label `v27-game-functions` 와 그 위 문단.
- [ ] **Step 4: tools** — `app/agents/qa/tools/game_function_tools.py`.
  - 공통: `step`, `thought` 를 받는다(다른 action tool 과 같다. `step` 이 현재 test case 를 정한다).
  - `run_cheat(step, name, thought, args={}, target=None)` → `invoke_cheat`.
  - `start_behavior(step, name, thought, args={}, target=None, max_seconds=30)` → `start_behavior`.
    `max_seconds` 1..300 은 보내기 전에 검사.
  - `stop_behavior(step, thought, behavior_id=None, all=False)` → `{"behaviorId", "reason":
    "stopped"}` 또는 `{"all": true, ...}`. 허용 검사 없음 — 멈추는 것은 늘 된다.
  - `extend_behavior(step, behavior_id, seconds, thought)`.
  - 거절 순서(전부 아무것도 안 보낸다): 상한 → run catalog 에 없는 이름 → test case 가 허용하지
    않는 이름 → static 인데 `target` 을 준 경우 → `target` 해석 실패.
  - `target` 해석: 그 function 의 `PULSE.functions` entry 의 `target` 중에서 찾고, 없으면
    `pulse` 의 live 객체 selector/path 중에서. 정확히 같으면 그것, 아니면 대소문자 무시 부분
    일치가 하나일 때만. 여럿이면 최대 5개를 댄다.
  - 보내기는 `ctx.act` 로, `ToolContext._act` 에 `describe` callback 을 하나 더해 성공 줄에
    `returnValue` 를 싣는다(`message`, `behaviorId`, `stopped`, `maxSeconds`). 결과에는 늘처럼
    `pulse` view 가 붙는다 — cheat 뒤에 보라는 규칙이 이것으로 지켜진다.
  - 상태: `GameFunctionSession` (catalog, step 별 `CaseFunctions`, 이번 `report_step` 이후 쓴
    cheat 목록, 호출 횟수). `QaRunState.game_functions` 에 둔다.
  - `start_behavior` 성공 시 `pulse.behaviors` 에 임시 줄을 넣어 다음 `PULSE` 전에도 보이게 한다.
  - phase: `run_cheat`, `start_behavior` 는 `ACT`. `stop_behavior`, `extend_behavior` 는 표에
    안 넣어 검사를 지나간다(`load_skill` 과 같은 이유 — 어느 phase 에서든 필요하고,
    `ALWAYS_ALLOWED` 에 넣으면 locked `phase_directive` 가 그 이름을 안 댄다).
  - `build_tools`: 축이 켜졌을 때만, 맨 뒤에 `run_cheat` 다음 behavior 셋. behaviors 축이 켜져
    있으면 모든 tool 을 감싸 `BEHAVIOR_ENDED` 를 결과 끝에 붙인다(phase gate 바깥).
- [ ] **Step 5: report_step** — `STATUS` 에 `used_cheats` (cheats 축이 켜졌을 때), `active_behaviors`
      (behaviors 축). 결과에 아직 도는 behavior 를 적는다. step+1 이 다른 test case 의 첫 step 이면
      그 case 가 허용하지 않는 running behavior 마다 `stop_behavior(reason="not_allowed")` 를
      보내고 결과에 적는다. `pulse.function_view` 를 step+1 기준으로 옮긴다.
- [ ] **Step 6: runner** — 첫 message 끝에 catalog (`render_catalog`), 축이 켜진 것만.
      시작할 때 `function_view` 를 step 1 기준으로 둔다.
- [ ] **Step 7: prompt v24** — v23 을 복사해 `version` 만 바꾸고, `tool_run_cheat.md`,
      `tool_start_behavior.md`, `tool_stop_behavior.md`, `tool_extend_behavior.md` (각 500자 이하),
      `skill_cheats.md`, `skill_behaviors.md` (description 300자 이하). skills directive 는 skill
      description 으로 목록이 생기므로 파일을 안 고친다. lock 재생성.
- [ ] **Step 8: Tests**
  - `test_qa_arch.py`: label/fingerprint 핀, 축 기본 `off`, 켜면 tool 이 맨 뒤에 붙고 fingerprint
    가 움직인다, `tool_call_limit`, skill withheld.
  - 새 `tests/test_qa_game_functions.py`: 거절 다섯 가지(아무 frame 도 안 나간다), `target` 확장,
    wire params, `returnValue` 문장, `BEHAVIOR_ENDED` 가 다음 tool 결과에 붙는다, `report_step`
    의 `used_cheats`/`active_behaviors` 와 결과 문장, test case 경계에서 `not_allowed` stop,
    `pulse` 줄(허용된 것만, 축 꺼지면 없음), `PulseMemory` merge/removed/whole, 첫 message catalog,
    `deliver` 가 `BEHAVIOR_ENDED` 를 받는다, session open 이 `context.cheats` 와 `arch.cheats` 를
    받는다, `STATUS` 에 축이 꺼지면 두 key 가 없다.
  - `test_qa_prompt_version.py`: 기본 version `v24`, v24 가 v23 의 role 을 그대로 갖는다.
  - `test_qa_phase_cycle.py`: 지시문 대조 목록에 `v24`.
  - prompt lock, run config contract.

## Validation

- `LANGSMITH_TRACING=false .venv/bin/python -m pytest -q` (환경에 따라 `env -u OPENROUTER_API_KEY`).
- 기대: 변경 전과 같은 기존 실패만 남는다.

## Risks & Rollback

- `pulse` 의 `behaviors` 는 1초 batch 라 방금 시작한 behavior 가 한 번 빠질 수 있다. 시작 결과로
  임시 줄을 넣어 줄인다. 이미 끝난 id 에 `not_allowed` stop 을 보내면 SDK 가
  `unknown behavior id` 로 답할 뿐이다.
- 축 기본이 `off` 라 orchestration 이 `arch.cheats` 를 보내기 전에는 어떤 run 도 바뀌지 않는다.
  되돌리기는 `git revert`.
- `QA_PROMPT_VERSION` 을 v23 이하로 고정한 배포에서 축을 켜면 tool 설명 파일이 없어 session open
  이 실패한다. 그 조합은 거절이 맞다.

## Open Questions

- 설계 plan 두 개가 없어 `pulse` view 줄의 정확한 문구와 `used_cheats` 를 묶는 단위(이번
  `report_step` 이후)는 이 plan 이 정한다.

## Plan review (2026-10-14)

fast·medium 은 subagent 한 번으로, heavy 는 직접 했다. 반영한 것:

- instance function 에 `target` 을 안 주면 `null` 로 보낸다. 고르는 것은 SDK 다(`no active <Component>`,
  `<n> ... objects; pass target as one of`). `target` 을 줬을 때만 해석한다.
- `target` 해석은 그 function 의 `PULSE.functions` entry 의 `target` 만 본다. live 객체 selector 로는
  내려가지 않는다 — 계약이 live instance 마다 entry 를 하나씩 싣는다.
- `used_cheats` 의 `target` 은 해석한 전체 selector, `success` 는 그 `ActionResultItem.success`.
  보내지 않고 거절한 호출은 안 적는다. 답이 안 온 호출은 `success: false`.
- `active_behaviors` 는 `pulse.behaviors` 에서 `behaviorId`, `name`, `target`, `seconds` 만 옮긴다.
  `start_behavior` 가 넣는 임시 줄은 `seconds: 0` 이다.
- `CaseFunctions`: 빈 목록은 `"none"` 과 같다. case 목록에 있어도 run catalog 에 없는 이름은 계속
  거절한다(좁히기만 한다). 거절문은 교집합만 댄다. case 가 없는 step 은 run catalog 전부다.
- `arch.cheats="on"` 인데 `context.cheats` 가 없으면 tool 은 있고, 모든 호출을 "이 run 에 준 cheat 가
  없다" 로 거절한다. session open 은 거절하지 않는다.
- catalog 는 session open 의 것으로 고정한다. 런 중에 SDK 가 `Register` 한 function 은 `pulse` 줄에는
  보여도 run catalog 에 없어 거절된다.
- `functions.whole` 이면 entry 를 통째로 바꾼다. 다시 연결된 SDK 는 첫 `PULSE` 에 `whole` 을 싣는다.
- step+1 이 마지막 step 을 넘으면 stop 하지 않는다. case 경계는 `report_step` 에서만 본다.
- `extend_behavior` 의 `seconds` 는 1 이상만 보낸다. 300초 합계는 SDK 가 검사한다.
- 마지막 tool 결과 뒤에 온 `BEHAVIOR_ENDED` 는 붙일 자리가 없어 버린다.
- 실제 QA run 을 못 돌렸다는 것을 보고에 적는다.

거절한 것:

- allowance 두 개를 없애자는 의견: 작업 지시가 `tool_call_limit` 에 넣으라고 한다. 축이 꺼진 run 의
  상한은 안 바뀐다.
- `BEHAVIOR_ENDED` 를 `ToolContext.answer` 에서 붙이자는 의견: 거절문이나 phase gate 의 거절처럼
  `answer` 를 안 지나는 결과가 있어서, 그 경우 소식이 다음 결과로 밀린다. 감싸는 쪽이 모든 결과를 덮는다.
- `start_behavior` 의 임시 줄을 없애자는 의견: 시작 직후 1초 안에 `report_step` 이 오면 그 behavior 가
  `active_behaviors` 에서도, case 경계의 `not_allowed` stop 에서도 빠진다.
- skill 하나로 합치자는 의견: 작업 지시가 둘을 요구하고 쓰는 시점이 다르다.
