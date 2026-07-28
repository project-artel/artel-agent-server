# 2026-07-26 — QA 에이전트 진단·루프·프로토콜 정리 (A/B/C/D/E)

- Date: 2026-07-26
- Jira: None (그룹별로 나눠 생성 예정)
- Status: Draft

## Goal

QA 런이 자기 상태를 관찰 가능하게 만들고, 오늘 실제로 죽은 두 원인(프레임 하나로
소켓이 닫히는 것, 같은 액션을 43회 반복하다 recursion limit에 걸리는 것)을 차단하며,
씬 조회 경로를 SDK 정식 방식인 `scan_scene` 액션 하나로 통일한다.

전부 2026-07-26 실행 데이터(`qa_try` 최신 런, 451 로그행)에서 확인된 문제다.

## Non-goals

- 전체 시나리오 end-to-end 완주 보장. 이번 변경은 진단·차단이지 성공률 개선이 아니다.
- SDK(C#) 변경. `ArtelManager.cs:270`이 이미 `scan_scene`을 받으므로 불필요하다.
- 오케 SSE 동시 append 유실(F1), `language`/`locale` 명명 통일(F2). 별건.
- 에이전트 컨텍스트 구조 문서(F5). 게재 위치 미정.

## Context / Constraints

### 관측된 사실 (2026-07-26)

| 관측 | 근거 |
|---|---|
| 앱 로그가 전부 유실 | `app/`에 `basicConfig`/`dictConfig` 0건. 로그 파일에 `[QA]` 0줄 |
| ACTION_RESULT 검증 실패가 WS를 닫음 | 실제 traceback → `QA Agent connection closed` |
| 자동 추론 캡처 0건 | THOUGHT 48줄 = 액션 48개. 전부 툴의 `thought` 인자 |
| `observe_scene` 5회 전부 무음 | `thought` 파라미터 없음 |
| 같은 액션 43회 → recursion limit 110 | THOUGHT 집계에서 동일 문장 43건 |
| 타임라인이 봉투를 SDK 프레임인 양 표시 | `ORCHE_TO_SDK GAME_STATE` payload = `{"step":null,"reason":...}` |
| `step`이 항상 null | 위 payload |
| `POST /gamestate/scene` 404 | QA 미처리 프레임의 레거시 폴백 |

### 제약

- **`asyncio.sleep`은 툴 안에서 안전하다.** `qa_sessions.py`가 `service.run`을
  `asyncio.create_task`로 띄우므로 수신 루프를 막지 않는다. E의 전제.
- **모델에 닿는 유일한 채널은 ToolMessage다.** `channel.note()`는 타임라인 부수효과일
  뿐 LangGraph 상태에 들어가지 않는다. 개입 문구는 반드시 툴 반환값에 실어야 한다.
- **SDK는 두 경로를 다 받는다.** `ArtelManager.cs:270`이 `scan_scene`(JSON-RPC)과
  `GET_GAME_STATE`(최상위)를 모두 처리하고, 에러 문구가 `scan_scene`을 정식으로
  안내한다. 따라서 오케에서 최상위 경로를 걷어내도 SDK 변경이 없다.
- 커밋은 `.agents/docs/commit.md`의 Conventional Commits(한글 본문)를 따른다.

## Approach (Checklist)

### Step 0: Recon — 완료

- [x] 실행 로그·DB(`qa_log` 451행)에서 8개 문제 확인
- [x] `ArtelManager.cs:270` — SDK가 `scan_scene` 수용 확인
- [x] `qa_sessions.py:93` — run이 별도 task임을 확인
- [x] `GameStateMessageHandler.kt:40` — `AgentClient`가 QA 미처리 폴백임을 확인

### Step 1: A — 진단 복구 (agent, 단독·작음)

- [ ] **A1** `app/logging_config.py` 신설. `configure_logging()`이 root 로거에
      핸들러/포맷/레벨을 붙이고 `app/main.py` 기동 시 호출. 레벨은 설정값
      (`LOG_LEVEL`, 기본 INFO)으로. uvicorn 로거와 이중 출력되지 않게 확인.
- [ ] **A2** `app/qa/service.py`의 `deliver()`가 `ValidationError`를 잡아 삼키고
      경고 로그를 남긴다. 프레임 하나가 런을 죽이지 않게. 오케의
      `QaAgentInboundRouter.kt:33`과 같은 방침.

### Step 2: C — 생각을 전부 남기기 (agent)

- [ ] **C1** `QaRunner._text_of`가 `reasoning`/`thinking` 블록도 취한다.
      텍스트가 없고 tool_call만 있는 턴은 호출 요약을 남긴다(무음 방지).
- [ ] **C2** `observe_scene`·`report_step`·`finish_run`·`reply_to_operator`에
      `thought` 추가하고 `channel.note(..., THOUGHT)`로 기록.

### Step 3: D — 타임라인이 사실을 말하게 (agent + orchestration)

- [ ] **D2** 모든 툴이 `step` 번호를 받아 프레임에 실는다. `QaRunState`에 현재
      step을 두지 말고 툴 인자로 명시(모델이 어느 step인지 스스로 밝히게).
- [ ] **D1** E와 함께 해소된다 — `REQUEST_GAME_STATE` 제거 시 봉투를 SDK 프레임인
      양 기록하던 `ORCHE_TO_SDK GAME_STATE` 행 자체가 사라진다. E 이후 잔여 확인.

### Step 4: E — 씬 조회를 액션으로 통일 (agent + orchestration)

- [ ] **E1-agent** `observe_scene`이 `REQUEST_GAME_STATE` 대신 `scan_scene`만 담은
      ACTION 배치를 보낸다. `wait_seconds`는 배치 전에 `asyncio.sleep`.
      `channel.request_scene()` / `RequestGameStatePayload` / `MessageType.REQUEST_GAME_STATE` 제거.
- [ ] **E1-orche** `QaAgentInboundRouter`에서 `REQUEST_GAME_STATE` 분기와
      `GET_GAME_STATE` 상수, `requestGameState()` 제거. `SUPPORTED_TYPES` 축소.
- [ ] **E2-orche** `AgentClient` 폴백 경로 정리. QA 미처리 프레임을 존재하지 않는
      엔드포인트로 POST하는 대신 debug 로그로 낮추거나 경로 자체를 제거.
      (제거 시 `ArtelWebSocketIntegrationTest`의 `sendResult` 검증 동반 수정)

### Step 5: B — 루프 차단 (agent)

- [ ] **B1-감지(결정적)** `QaRunState`에 액션 저널을 두고 `tools.py`의 `_run`
      헬퍼에서 판정한다(행동 툴 3개가 모두 거길 지난다). 판정은 순수 함수로 분리해
      LLM 없이 테스트 가능하게.

      **주의: "씬 변화 없음"은 트리거로 쓸 수 없다.** 관측된 루프에서 씬은 매번
      변했다 — 내레이션이 한 글자씩 타이핑되어 `ChatText.content`가
      `"모든 준비를 마친 워드"` → `"모든 준비를 마친 워드는 복수의 여정을..."`으로
      계속 늘었다. 변화 없음을 조건에 걸면 43회 루프를 놓친다.

      대신 2단 트리거:

      - **T1 (좁고 빠름)** — `(method, params)`가 연속 N회(초안 5) 동일.
        관측 런의 `press_key("Space", ...)` 43연속이 여기 걸린다. 대사를 넘기려
        Space를 3회 누르는 것은 정상이므로 3은 이르다.
      - **T2 (넓고 느림)** — `progress = (씬 이름, report_step 호출 횟수)`가
        그대로인 채 액션 M회(초안 10). 관측 런은 씬이 계속 `StoryScene`이고
        `report_step`이 초반 2회 후 0회였다. 매번 다른 키를 시도하며 헤매는
        경우처럼 T1이 놓치는 정체를 잡는다.

      둘 중 하나라도 걸리면 개입한다.
- [ ] **B1-개입(LLM)** 트리거 시 별도 1회 호출로 개입문 생성. 프롬프트에는
      **씬 변화 원본과 액션 이력만** 주고 역할을 "막힌 에이전트를 진단하는 관찰자"로
      고정한다. 에이전트 자신의 `thought`는 넣지 않는다 — 43회 전부 같은 문장이었던
      것이 그 서술로는 맹점을 못 깬다는 증거다.
- [ ] **B1-폴백/상한** LLM 호출 실패·타임아웃 시 고정 문구.

      개입은 계단식으로. 임계를 넘겼다고 매 턴 붙이면 컨텍스트가 개입문으로
      도배된다:

      | 시점 | 동작 |
      |---|---|
      | N회 | 개입문을 툴 결과에 첨부 |
      | 2N회 | 개입문 재생성 + "다음에도 같으면 이 step은 실패 처리된다" 경고 |
      | 3N회 | 툴이 강제로 실패 반환 |

      상한은 필수다. 조언만으로 멈추지 않는 경우가 있다.
- [ ] **B2** `trim_messages` 최소 적용. 이번엔 GAME_STATE 평균 235자로 토큰이
      병목이 아니었으므로 보수적으로.

## Validation

- **Commands to run:**
  - agent: `uv run --extra dev pytest -q` (현재 baseline 82 passed)
  - orchestration: `./mvnw -q test`
  - 수동: 세 서비스 기동 후 Unity 붙여 QA 1회 실행
- **Expected output:**
  - A1: 콘솔에 `[QA] run starting` / `[QA] model turn` / `[QA] tool result`가 실제로 찍힌다
  - A2: 스키마 불일치 프레임을 넣어도 런이 계속된다
  - C: `observe_scene`을 포함한 모든 툴 호출에 THOUGHT 로그가 1:1로 대응
  - D: `qa_log`에 `step`이 채워지고, 봉투를 SDK 프레임으로 표시하는 행이 없다
  - E: `qa_log`에 `REQUEST_GAME_STATE`/`GET_GAME_STATE`가 0건, `scan_scene` 액션으로만 조회
  - E2: 런 중 `POST /gamestate/scene 404`가 더 이상 찍히지 않는다
  - B: 같은 액션 반복 시 툴 결과에 개입문이 붙고, 상한에서 런이 정상 종료된다

### 신규 테스트

- `tests/test_qa_logging.py` — 앱 로거가 실제로 출력을 낸다 (A1 회귀 방지)
- `tests/test_qa_channel.py` 보강 — 잘못된 프레임이 예외를 밖으로 내지 않는다 (A2)
- `tests/test_qa_reasoning_log.py` 보강 — reasoning/thinking 블록, tool-call-only 턴 (C1)
- `tests/test_qa_tools.py` 보강 — 모든 툴의 thought 기록, step 전달, scan_scene 경로 (C2/D2/E1)
- `tests/test_qa_repetition.py` 신설 — 감지 트리거, LLM 실패 폴백, 상한 강제 종료 (B1)

## Risks & Rollback

- **Risks:**
  - **E는 오케-에이전트 동시 배포가 필요하다.** 에이전트만 먼저 나가면
    `REQUEST_GAME_STATE`를 아무도 안 보내니 무해하지만, 오케만 먼저 나가면 구버전
    에이전트의 요청이 `Unsupported Agent message type`으로 떨어진다. → **에이전트 먼저.**
  - B1의 LLM 개입은 비결정적이라 테스트가 트리거·폴백·상한(결정적 부분)에 한정된다.
    개입문 품질 자체는 수동 확인 영역.
  - A1이 로그를 켜면 기존에 조용하던 경로에서 대량 출력이 나올 수 있다. 레벨 설정으로 제어.
  - E2에서 `AgentClient`를 제거하면 QA 밖 게임 상태를 받던 소비자가 있었을 경우 끊긴다.
    현재 에이전트에 해당 엔드포인트가 없어 404이므로 실질 소비자는 없다고 판단.
- **Rollback steps:** 그룹별로 커밋을 나눠 A/C/D/E/B를 독립 revert 가능하게 유지.
  E는 오케+에이전트 두 레포라 revert도 짝으로.

## Open Questions

- B1 임계값 초안(T1 N=5, T2 M=10, 상한 3N)이 적절한가. 실행 데이터가 더 필요하다.
  특히 정상적인 대사 넘기기가 몇 회까지 이어지는 게임인지에 따라 T1이 오탐할 수 있다.
- E2의 `AgentClient`를 완전히 제거할지, debug 로그로 낮춰 남길지.
- 그룹별 Jira 이슈를 A~E 각각 팔지, A+C+D를 하나로 묶을지.
