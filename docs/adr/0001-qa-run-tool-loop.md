# ADR 0001 — QA 실행을 structured output 두 번 호출에서 tool loop 로 바꾼다

- 상태: 확정
- 근거: `app/agents/qa/runner.py`, `app/agents/qa/arch.py`, `app/agents/qa/tools/__init__.py`,
  [`.plan/general/2026-07-25-qa-agent-create-agent-tool-loop.md`](../../.plan/general/2026-07-25-qa-agent-create-agent-tool-loop.md) (ARTEL-85),
  [`.plan/general/2026-07-26-qa-agent-diagnostics-loop-and-protocol-cleanup.md`](../../.plan/general/2026-07-26-qa-agent-diagnostics-loop-and-protocol-cleanup.md)

## 결정

- QA 런은 `langchain.agents.create_agent` 가 도는 tool loop 입니다. 서비스가 루프를 돌리지
  않고, agent 가 필요할 때 씬을 요청합니다.
- tool 은 37 개, middleware 는 5 개이고 **둘 다 순서가 계약입니다.**
  `app/qa/run_config.py` 가 그 순서를 run config 에 저장하고 모델도 그 순서로 받습니다.
- 상한이 둘입니다 — tool 호출 수와 벽시계.
- 씬 전환은 **씬 이름 변화로만** 판정합니다.

## 왜

### 1. 옛 구조는 게임이 먼저 말을 걸어야 움직였습니다

- `QaExecutionService` 가 인바운드 메시지 하나를 받아 프레임 목록을 돌려주는 요청/응답
  핸들러였고, agent 는 `act` 와 `evaluate` 두 번의 구조화 출력 호출뿐이었습니다.
- 즉 **게임이 `GAME_STATE` 를 보내야만 진행됐습니다.**
  - 아무것도 보내지 않는 게임 앞에서는 런이 시작조차 못 했습니다.
  - plan 문서가 적은 실측: `qa_try 16` 은 `STATUS` 2 건과 `CHAT` 뿐이고
    `GAME_STATE` 와 `ACTION` 이 0 건이었습니다.
- tool loop 은 그 의존을 끊습니다. 화면이 아직 준비되지 않았으면 agent 가 몇 초 기다렸다
  다시 보고, 한 스텝 안에서 여러 번 관측하고 행동합니다.

### 2. 상한이 하나면 어느 쪽이든 샙니다

- **호출 수만 두면** 답 없는 게임에서 한 호출이 오래 매달려 시간이 무한정 늘어납니다.
- **시간만 두면** 짧은 시간에 호출을 폭발시키는 경우를 못 막습니다.
- 그래서 둘 다 두고 먼저 걸리는 쪽에서 끝냅니다.

오늘 값은 plan 문서가 정한 값이 아닙니다. **코드가 이깁니다.**

| 무엇 | plan (2026-07-25) | 코드 (`app/agents/qa/arch.py`) |
| --- | --- | --- |
| tool 호출 | `10 + 15 × 스텝수` | `BASE_TOOL_CALLS` 1,000 + `TOOL_CALLS_PER_STEP` 1,000 × 스텝수 |
| 벽시계 | 10 분 | `RUN_DEADLINE_SECONDS` 86,400 초 |

- 숫자가 느슨해진 이유를 코드가 적습니다 — 두 상한은 이제 배급이 아니라 폭주 방지입니다.
  - 배급이던 시절에 나온 것은 **시나리오 중간에 답 없이 멈춘 런**이었고,
    그것이 느린 런보다 나쁜 유일한 결과입니다.
  - 배급은 tool 설명으로 옮겼습니다.
- 두 상한이 걸렸을 때 남는 말이 다릅니다.

| 무엇이 걸리나 | 무엇이 기록되나 |
| --- | --- |
| 벽시계 | `The run exceeded its {N}s limit.` |
| tool 호출 수 | `The run stopped on an error: {error}` — LangGraph 의 `GraphRecursionError` 가 일반 예외 자리로 떨어짐 |

### 3. 씬 전환을 interactable 구성으로 세면 근거가 지워집니다

- 전환마다 누적된 관측을 초기화하는데, interactable 구성 변화까지 전환으로 보면
  **화면이 바뀌는 바로 그 순간에 이전 값이 사라집니다.**
- 「누르면 상점 화면이 뜬다」 같은 스텝은 정확히 그 전후 비교를 근거로 삼습니다.
  그때 초기화하면 판정 근거를 스스로 지우는 셈입니다.
- 그래서 이름 변화만 전환으로 봅니다. 잔상은 `missing` 키에 수명을 두어 처리합니다.

### 4. 「씬 변화 없음」은 반복 루프의 신호가 못 됩니다

- 2026-07-26 에 `press_key("Space", ...)` 를 **43 회** 반복해 recursion limit 110 에
  부딪힌 런이 있었습니다.
- 그 런에서 **씬은 매번 변했습니다.** 내레이션이 한 글자씩 타이핑돼
  `ChatText.content` 가 계속 늘어났기 때문입니다.
- 변화 없음을 조건에 걸면 그 43 회 루프를 놓칩니다. 그래서 반복 판정의 근거는
  씬이 아니라 같은 `(method, params)` 가 연속으로 나온 횟수여야 합니다.
- **다만 그 detector 는 오늘 코드에 없습니다.** plan 이 설계한 T1/T2 단계별 개입은
  구현되지 않았고, 멈춘 런을 끝내는 것은 위의 두 상한뿐입니다.

## 거절한 대안

| 대안 | 왜 거절했나 |
| --- | --- |
| 구조화 출력 두 번 호출을 유지 | 게임이 먼저 말을 걸어야 움직임. 아무것도 안 보내는 게임 앞에서 영원히 멈춤 |
| 상한을 하나만 둠 | 호출 수만이면 시간이 새고, 시간만이면 호출이 폭발함 |
| 씬 전환을 interactable 구성 변화로 판정 | 전후 비교를 근거로 삼는 스텝이 자기 근거를 지움 |
| 「씬 변화 없음」을 반복 루프 트리거로 | 실측 43 회 루프에서 씬은 매 프레임 변했음 |
| 옛 구조를 코드에 병렬로 남겨 비교 | 관리되지 않는 복사본은 주변 tool 시그니처가 움직이는 동안 썩음. 옛 구조는 commit 과 image tag 로 재현함 |
| 실행 재개(resume) | Agent WebSocket 이 끊기면 orchestration 이 try 를 FAILED 로 만듦. 루프 수명이 곧 소켓 수명임 |

## 대가

| 대가 | 무엇이 일어나나 |
| --- | --- |
| tool 이 두 홉을 왕복함 | `observe_scene` 도 `click_button` 도 답이 **다른 WebSocket 메시지**로 옴. `QaRunChannel` 이 `correlationId` 로 future 를 풀어 줘야 tool 이 `await` 할 수 있음 |
| tool 안에서 기다리면 세션 전체가 멈춤 | 세션 하나가 수신 루프 하나를 씀. 대기는 orchestration 에 맡김 |
| 반복 루프를 진단하는 자리가 없음 | 43 회 같은 루프는 tool 호출 상한이 소진될 때까지 돎. 그 끝이 일반 오류로 보고돼 다른 crash 와 구분되지 않음 |
| 순서가 계약이라 재배열이 곧 구조 변경 | tool 을 위아래로 옮기면 `arch_fingerprint` 가 달라지고 옛 런과 묶이지 않음. middleware 도 같음 |
| `recursion_limit` 이 tool 호출 상한의 배수 | graph step 을 세므로 `tool_call_limit × (2 + compaction 이면 1)` 로 환산해 넘김 |
