# 2026-07-25 — QA 실행 에이전트를 create_agent 툴 루프로 전환

- Date: 2026-07-25
- Jira: ARTEL-85
- Status: Draft

## Goal

QA 실행 에이전트를 `langchain.agents.create_agent` 툴 루프로 바꾼다. 에이전트가 스스로
관찰하고 행동하도록 만드는 것이 목적이다.

- 게임이 상태를 보내 줄 때까지 기다리지 않고 에이전트가 필요할 때 씬을 요청한다.
- 화면이 아직 준비되지 않았으면 몇 초 기다렸다가 다시 본다.
- 한 스텝 안에서 여러 번 관찰·행동할 수 있다. 지금은 스텝당 액션 한 번으로 고정돼 있다.
- 운영자의 개입이 다음 툴 호출에 반영된다.

## Non-goals

- 시나리오 생성 에이전트(`app/agents/scenario/`)는 건드리지 않는다. 잘 동작하고 있고,
  같이 바꾸면 실패 원인이 섞인다.
- 엔벨로프 계약 변경. `REQUEST_GAME_STATE`/`ACTION`/`STATUS`/`CHAT`은 이미 있는 것을
  그대로 쓴다.
- 프론트엔드 변경.
- 실행 재개(resume). 아래 제약 참고.

**범위에 포함(결정됨).** Orchestration의 `AgentGameState`에 액션 텔레메트리를 추가한다.
판정 근거를 만드는 것이 이 작업의 핵심인데 가장 직접적인 근거가 지금 버려지고 있다.
아래 "액션 텔레메트리" 절 참고.

## Context / Constraints

**현재 구조.** `QaExecutionService`가 인바운드 메시지 하나를 받아 프레임 목록을 돌려주는
요청/응답 핸들러다. 진행 상태는 `QaSessionRecord.phase`(`need_action` / `need_result` /
`done`)로 Redis에 있다. 에이전트는 `act`/`evaluate` 두 번의 구조화 출력 호출뿐이고,
루프는 서비스가 돌린다. 즉 **게임이 GAME_STATE를 보내야만 진행된다.** 실제로 게임이
상태를 보내지 않아 실행이 시작조차 못 한 사례가 있었다(qa_try 16: STATUS 2건과 CHAT뿐,
GAME_STATE·ACTION 0건).

**핵심 난점.** 툴 루프는 툴이 값을 즉시 반환한다고 가정한다. 그런데 여기 툴은 두 홉을
왕복한다.

```
observe_scene()   → REQUEST_GAME_STATE → Orchestration → SDK → ... → GAME_STATE 로 도착
perform_actions() → ACTION             → Orchestration → SDK → ... → ACTION_RESULT 로 도착
```

응답이 **다른 WS 메시지로** 온다. 따라서 툴이 await 할 수 있게 인바운드 메시지가 future를
resolve 하는 구조가 필요하다.

**제약.**

- 세션 하나는 WS 수신 루프 하나를 쓴다. 툴 안에서 `sleep` 하면 그 실행의 다른 메시지가
  전부 멈춘다. 대기는 지금처럼 `REQUEST_GAME_STATE.after_seconds`로 Orchestration에
  맡긴다(`QaAgentInboundRouter.requestGameState`가 `Mono.delay`로 예약).
- Orchestration은 Agent WS가 끊기면 try를 FAILED로 만든다(`agentDisconnected`).
  그러므로 **에이전트 루프는 WS 수명과 같다.** Redis의 레코드로 재개하는 경로는 지금도
  실질적으로 없다.
- `langchain==1.3.14`, `langgraph==1.2.9`를 새로 의존성에 추가했다.
  `create_agent(model, tools, *, system_prompt, response_format, ...)` →
  `CompiledStateGraph`. async 툴을 지원한다.
- 모델은 OpenRouter를 통한 `ChatOpenAI`다(`app/llm/chat_model.py`). 툴 호출을 지원하는
  모델이어야 한다. 기본값 `openai/gpt-4o-mini`는 지원한다.

## 게임 상태 설계 — 누적과 변화 추적

**문제.** 지금은 프레임이 올 때마다 `record.latest_game_state`를 통째로 갈아치운다. 그래서
에이전트는 "지금 화면"만 볼 뿐 **무엇이 바뀌었는지 모른다.** 스텝 판정(`expected`)은 대개
변화에 대한 진술("점수가 오른다", "다이얼로그가 닫힌다")이라, 스냅샷만으로는 판정 근거가
없다. 액션 전후를 비교할 수 있어야 한다.

**전제 (코드에서 확인한 사실).**

- `observables`는 `{"노드명.content" | "노드명.컴포넌트타입.상태명": {value, type}}` 형태다
  (`GameStateTransformer.traverse`). 키가 노드/컴포넌트 이름에서 나오므로 **프레임 간
  안정적**이고, 그래서 키별 값 추적이 성립한다.
- `scene`은 씬 트리 루트 노드의 이름이다. 씬이 바뀌면 **키 공간 자체가 갈린다** —
  이전 씬의 관찰값을 들고 있어 봐야 의미가 없다.
- `interactables`의 `id`는 노드 id다. 프레임마다 달라질 수 있으므로 **행동의 근거는 항상
  최신 프레임**이어야 한다.

**모델** — `app/qa/scene.py` (신규)

```python
class Observation:
    at: int       # 몇 번째 관찰에서 이 값이 됐는지
    value: Any

class ObservableTrack:
    type: str
    values: list[Observation]   # 값이 바뀐 시점만, 오래된 것부터. 상한 있음

class SceneMemory:
    scene: str
    updates: int                              # 병합한 프레임 수
    interactables: list[Interactable]         # 최신 프레임으로 교체
    observables: dict[str, ObservableTrack]   # 누적
    missing: list[str]                        # 전에 있었으나 이번 프레임에 없는 키
```

**값은 list 하나로 둔다.** 현재값·직전값·변화 횟수를 따로 들고 있으면 전부 list에서
계산되는 값을 중복 저장하는 것이고, 갱신을 한 군데라도 빠뜨리면 서로 어긋난다.

- 현재값 = `values[-1].value`
- 직전값 = `values[-2].value`
- 변화 횟수 = `len(values) - 1`
- **지난 관찰 이후 바뀌었나** = `values[-1].at > watermark`
- **그 사이 거쳐 간 값들** = `[o.value for o in values if o.at > watermark]`

마지막 두 개가 `at`을 넣는 이유다. 값만 담으면 "언제 바뀌었는지"가 사라져 지난 관찰
이후의 차이를 낼 수 없다. `Hp 100 → 80 → 60`처럼 한 번 안 본 사이 여러 번 바뀐 경로도
이 list가 그대로 보여 준다.

상한에 걸려 앞쪽이 잘린 키는 뷰에서 "이전 변화 일부 생략"이라고 밝힌다. 조용히 자르면
에이전트가 변화 횟수를 잘못 읽는다.

**병합 규칙** — `SceneMemory.apply(state)`

1. `state.scene != self.scene` → **전체 초기화**. 새 씬은 새 세계다. 이전 키를 남기면
   에이전트가 없는 요소를 근거로 판단한다.
2. 같은 씬이면 병합한다.
   - `interactables`는 **교체**한다. 조작 대상은 최신이 진실이다.
   - `observables`: 값이 달라진 키만 `history`에 쌓고 `previous`/`changes`를 갱신한다.
     같은 값이면 아무것도 하지 않는다 — 그래야 "변화"가 신호로 남는다.
   - 이전에 봤는데 이번 프레임에 없는 키는 **지우지 않고 `missing`으로 표시**한다.
     사라진 것 자체가 증거다(다이얼로그가 닫혔다 등).

**에이전트에게 주는 형태.** 원본 덤프가 아니라 **변화 중심 뷰**를 돌려준다. `observe_scene`
툴은 에이전트가 마지막으로 본 시점(watermark)을 기억하고 그 이후의 차이를 낸다.

```
scene: Lobby  (관찰 3회째)
지난 관찰 이후 바뀐 것:
  Score.text.value      0 → 100
  Dialog.content        (사라짐)
  Hp.slider.value       100 → 80 → 60   (2회 변화)
바뀌지 않음: 42개
조작 가능: [ {id, name, type, label} ... ]
```

이 뷰가 있으면 액션 직후 `observe_scene` 한 번으로 `expected` 검증 근거가 그대로 나온다.

**상한.** `history`는 키당 최근 N개(초안 10)만, 씬 초기화 시 함께 비운다. observables 수가
많을 수 있으므로(실측 42개) 바뀐 것은 전부 보여 주고 안 바뀐 것은 개수와 이름만 준다.

## 액션 텔레메트리 — Orchestration 변경

**문제.** SDK는 컴포넌트마다 실행 기록을 보낸다(`SdkAction`: sequence, tag, name, success,
returnValue, error, timeStamp). 그런데 `GameStateTransformer`는 **이름만** 남기고 나머지를
버린다. 그것도 커스텀 컴포넌트에만 해당하고, button·editText 분기는 actions를 아예 보지
않는다.

버려지는 것이 판정에 가장 직접적인 증거다.

- 지금 에이전트는 "관찰값이 변했으니 성공했겠지"라고 **추론**해야 한다. 텔레메트리에는
  실제로 무엇이 실행됐고 성공했는지, 무엇을 반환했고 왜 실패했는지가 그대로 있다.
- **에이전트가 시키지 않은 액션**(게임이 스스로 실행한 것)은 `ACTION_RESULT`로 오지
  않는다. 텔레메트리가 없으면 관측 자체가 불가능하다.
- ACT 프롬프트는 이미 "recorded action telemetry"를 근거로 삼으라고 지시한다. 즉 지금은
  **없는 것을 참조하라고 쓰여 있다.**

**변경.** `AgentGameState`에 최근 실행 기록을 싣는다.

```kotlin
data class AgentActionRecord(
    val target: String,        // 노드 이름
    val name: String,
    val success: Boolean,
    val returnValue: Any? = null,
    val error: String? = null, // SdkError.message
    val at: String             // timeStamp
)

data class AgentGameState(
    val scene: String,
    val interactables: List<Interactable> = emptyList(),
    val observables: Map<String, ObservableValue> = emptyMap(),
    val recentActions: List<AgentActionRecord> = emptyList()   // 신규
)
```

- 씬 전체를 훑어 모은 뒤 `sequence` 기준 최신 N개(초안 20)만 남긴다. 상한이 없으면
  프레임마다 커진다.
- 모든 컴포넌트에서 모은다. 지금처럼 커스텀 컴포넌트로 한정하지 않는다 — 버튼 클릭이야말로
  QA가 확인하려는 것이다.
- `Interactable.actions`(호출 가능한 메서드 이름)는 그대로 둔다. 그건 "무엇을 부를 수
  있는가"이고, 이건 "무엇이 실행됐는가"다. 서로 다른 정보다.

에이전트 쪽에서는 `GameState`에 같은 필드를 받고, 씬 메모리가 관찰 회차와 함께 누적해
`observe_scene` 뷰에 "지난 관찰 이후 실행된 것"으로 보여 준다.

## Approach (Checklist)

- [ ] **Step 0: Recon**
  - `app/qa/service.py`, `app/api/qa_sessions.py`, `app/agents/qa/*` 현재 흐름 확인
  - `QaSessionRecord`에서 루프가 흡수할 필드와 남길 필드 구분

- [ ] **Step 1: 전송 브리지** — `app/qa/channel.py` (신규)
  - `QaRunChannel`: 프레임 송신 + 인바운드 대기
    - `request_scene(after_seconds) -> GameState`: `REQUEST_GAME_STATE` 전송 후 future await
    - `dispatch_actions(actions) -> ActionResultPayload`: `ACTION` 전송 후 correlation 일치하는
      `ACTION_RESULT` await
    - `emit(frame)`: LOG/STATUS/CHAT 즉시 송신
    - `on_game_state` / `on_action_result` / `on_chat` / `on_cancel`: future resolve, 취소 신호
  - 타임아웃: 게임이 영영 답하지 않을 수 있다. `asyncio.wait_for`로 상한을 두고, 초과 시
    툴이 "응답 없음"을 값으로 돌려준다(예외로 루프를 죽이지 않는다 — 에이전트가 판단할 일)
  - 운영자 발화는 큐에 쌓고 **다음 툴 반환값에 덧붙여** 루프 안으로 넣는다. 그래프 실행
    중간에 메시지를 주입하는 것보다 단순하고, 개입이 반드시 다음 결정에 닿는다

- [ ] **Step 1.5: 씬 메모리** — `app/qa/scene.py` (신규)
  - `SceneMemory` / `ObservableTrack`과 병합 규칙(위 설계)
  - 렌더러: 변화 중심 뷰 문자열. watermark 이후의 차이를 낸다
  - 씬 전환 시 초기화. history 상한

- [ ] **Step 2: 툴 정의** — `app/agents/qa/tools.py` (신규)
  - `observe_scene(wait_seconds: float = 0)` — 현재 씬을 본다. 로딩 중이면 기다렸다가.
    반환은 원본 덤프가 아니라 **지난 관찰 이후의 변화 중심 뷰**다
  - `perform_actions(actions)` — SDK 액션 실행하고 결과를 받는다
  - `report_step(step, passed, message)` — 스텝 판정을 기록한다
  - `finish_run(passed, summary)` — 실행을 끝낸다(종단 STATUS)
  - `note(message)` — 관찰/추론을 타임라인에 남긴다
  - 툴은 채널을 클로저로 잡는다. 세션마다 새로 만든다

- [ ] **Step 3: 에이전트** — `app/agents/qa/agent.py` 재작성
  - `create_agent(model=chat_model, tools=..., system_prompt=...)`
  - 시스템 프롬프트: 시나리오 전체와 스텝 목록, SDK 메서드 목록, 언어 지시,
    "행동 전에 반드시 관찰한다", "스텝마다 report_step", "끝나면 finish_run"
  - 첫 입력: 시나리오와 실행 지시. 이후는 루프가 진행

- [ ] **Step 4: 세션 런타임** — `app/qa/service.py` 재작성
  - WS 연결 시 루프를 태스크로 기동(`asyncio.create_task`)
  - 인바운드 메시지는 핸들러가 아니라 채널로 전달
  - `phase` 상태기계 제거(루프가 상태를 들고 있음). `QaSessionRecord`는 감사/재시작 진단용
    최소 정보만 유지
  - 종료: 루프 완료, CANCEL 수신, WS 종료 → 태스크 취소 + 정리

- [ ] **Step 5: 테스트**
  - 채널: 요청/응답 짝, 타임아웃, correlation 불일치
  - 툴 루프: 가짜 채널 + 스크립트된 모델로 관찰→행동→판정→종료 한 바퀴
  - 회귀: 운영자 발화가 다음 툴 호출에 반영되는지

- [ ] **Step 6: Rollout**
  - 되돌리기는 `git revert` 한 번(이 브랜치의 커밋만)
  - Orchestration/프론트 변경 없음이므로 롤백 시 다른 repo 영향 없음

## Validation

- **Commands to run:**
  - `uv run --extra dev pytest -q`
  - 세 서버 기동 후 실제 QA 1회 실행(SDK 연결 상태에서)
- **Expected output:**
  - 테스트 전부 통과
  - qa_log에 `REQUEST_GAME_STATE` → `GAME_STATE` → `ACTION` → `ACTION_RESULT` → per-step
    `STATUS` → 종단 `STATUS` 순서가 남는다
  - 게임이 상태를 자발적으로 보내지 않아도 실행이 시작된다(현재 결함의 해소 확인)

## Risks & Rollback

- **Risks:**
  - **툴 루프가 멈추지 않을 수 있다.** 모델이 `observe_scene`만 반복하면 비용이 샌다.
    → 툴 호출 횟수 상한과 실행 시간 상한을 두고, 초과 시 FAILED로 종단한다.
  - **재개 불가.** 루프가 메모리에 있으므로 Agent 서버가 재시작되면 진행 중 실행은 잃는다.
    지금도 WS가 끊기면 Orchestration이 FAILED 처리하므로 실질적 후퇴는 아니지만,
    문서로 남긴다.
  - **모델 의존.** 툴 호출을 못 하는 모델을 고르면 실행이 안 된다. 모델 스펙에
    툴 지원 여부를 두고 세션 생성 시 거른다.
  - **응답 없는 게임.** SDK가 답하지 않으면 툴이 타임아웃으로 값을 돌려준다. 에이전트가
    재시도할지 종료할지 판단하게 하되, 전체 시간 상한이 최종 안전장치다.
- **Rollback steps:** `git revert` 후 이전 `QaExecutionService`로 복귀. 다른 repo 무관.

## Open Questions

- 스텝 판정을 `report_step` 툴로 받을지, `response_format`으로 구조화 출력을 받을지.
  툴 쪽이 중간 판정을 실시간으로 타임라인에 흘릴 수 있어 유리해 보이지만, 모델이
  호출을 빠뜨릴 수 있다.
- 툴 호출 상한과 실행 시간 상한의 구체 값. 시나리오 스텝 수에 비례해 잡을지.
- 씬 이름이 같은데 화면이 실제로 바뀌는 게임이면 초기화가 걸리지 않는다. 루트 이름 외에
  interactables 구성 변화까지 봐서 씬 전환으로 볼지.
- `missing` 키를 몇 프레임까지 들고 있을지. 영원히 남기면 오래된 씬의 잔상이 쌓인다.
