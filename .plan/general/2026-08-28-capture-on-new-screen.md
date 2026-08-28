# 2026-08-28 — 새 screen 판정 시 capture 를 자동 발행한다

- Date: 2026-08-28
- Jira: ARTEL-595
- Status: Implemented
- Base: `develop` (155485b)

## Goal

orchestration 이 `pulse` 에서 **처음 보는 screen**을 만든 그 순간, agent-server 가
`capture_screen` action 을 한 번 내고 그 결과(`url`, `captureId`)를 screen id 와 함께
orchestration 으로 돌려준다. screen 행이 자기 모습을 가진 채로 태어난다.

## 계약이 아직 없다 — 이 PR 이 정의한다

ARTEL-453 의 `ScreenObservationService` 는 screen 행을 만들고 **아무에게도 알리지 않는다.**
`QaSdkBridgeService` 가 agent 로 내보내는 타입은 `GAME_STATE` · `PULSE` ·
`ACTION_RESULT` · `CANCEL` · `KNOWLEDGE_*_RESULT` 뿐이고, `QaAgentInboundRouter` 의
`SUPPORTED_TYPES` 에도 screen 과 관련된 타입이 없다.

그래서 frame 두 개를 **여기서 정의하고, 그 정의에 맞춰 구현한다.** 있는 것처럼 쓰지
않는다 — orchestration 쪽 절반은 ARTEL-456 이 붙인다.

### `SCREEN_CREATED` (Orchestration → Agent)

처음 보는 screen 행이 생긴 그 순간, 그 screen 당 **정확히 한 번**.

```json
{
  "type": "SCREEN_CREATED",
  "qaTryId": "7",
  "messageId": "3f2a...-uuid",
  "payload": { "screenId": "12", "sceneName": "TitleScene" }
}
```

- 재방문에는 보내지 않는다. `screen.observe` 는 upsert 라 삽입과 갱신을 구분하지 않으므로
  orchestration 쪽에서 `RETURNING id, (xmax = 0) AS inserted` 같은 것으로 삽입만 골라야 한다
- `sceneName` 은 타임라인 문구에만 쓴다. 없어도 동작한다

### `SCREEN_CAPTURE` (Agent → Orchestration)

`SCREEN_CREATED` 의 `messageId` 를 `correlationId` 로 싣는다.

```json
{
  "type": "SCREEN_CAPTURE",
  "correlationId": "3f2a...-uuid",
  "payload": {
    "message": "Captured new screen 12",
    "screenId": "12",
    "captureId": "cap-1",
    "url": "https://.../cap-1.jpg",
    "mimeType": "image/jpeg"
  }
}
```

- `message` 를 싣는 것은 `QaAgentInboundRouter` 가 특별히 분기하지 않는 타입에 대해
  `payload.message` non-blank 를 요구하기 때문이다. 그 가드를 지나야 frame 이 산다
- capture 가 실패하면 이 frame 을 **아예 내지 않는다.** 묶을 것이 없는 frame 을 보내는 것보다
  타임라인에 이유를 남기는 쪽이 정직하다
- ARTEL-456 이 `SUPPORTED_TYPES` 에 이 타입을 넣기 전까지 orchestration 은 이 frame 에
  `Unsupported Agent message type` 으로 답한다. 그 ERROR 는 correlation 이 풀 것이 없어
  로그로만 남고 런은 그대로 돈다

## Non-goals

- 재방문 시 이미지를 갱신하지 않는다. 처음 것만 남긴다 (ARTEL-587 / ARTEL-456 의 결정)
- 이 capture 의 이미지를 모델에게 보이지 않는다. 지도의 그림이지 스텝 판정의 근거가 아니다
- agent 가 스텝 판정을 위해 찍는 `capture_screen` 도구의 규칙은 건드리지 않는다
- orchestration 을 건드리지 않는다. 발행하는 절반만 여기 있다

## Context / Constraints

- **도구가 아니다.** 모델이 부르지 않고 `arch.max_captures_per_run` 도 쓰지 않는다.
  구조적으로 그렇다 — 이 경로는 `QaRunState` 를 아예 손에 쥐지 않는다
- **실패가 screen 을 막지 않는다.** capture 실패·무응답·읽을 수 없는 `returnValue` 는 모두
  타임라인 한 줄로 끝난다. screen 행은 orchestration 이 이미 만들었다
- **소켓을 읽는 쪽을 막으면 안 된다.** `deliver` 는 동기 콜백이라, capture 왕복을 거기서
  기다리면 그동안 pulse 도 action 결과도 채널에 못 들어온다. 백그라운드 task 로 띄운다
- **action 을 내는 곳이 둘이 되었다.** 종전 `QaRunChannel` 은 나간 ACTION 하나의 future 를
  필드로 들고 있었다. 도구와 자동 capture 가 겹치면 뒤의 것이 앞의 것을 덮어써서, 앞의
  action 은 자기 답이 와도 못 받고 타임아웃까지 앉아 있다가 "게임이 답하지 않았다"가 된다.
  future 를 나간 frame 의 `messageId` 로 키잡아 그 겹침을 없앤다
- **자동 capture 끼리는 한 줄로 세운다.** screen 이 빠르게 갈리면 `SCREEN_CREATED` 가 연달아
  오는데, 그때마다 SDK 배치를 동시에 밀면 게임 쪽에 몇 개가 떠 있는지 아무도 모른다

## Approach (Checklist)

- [x] `app/qa/envelope.py` — `MessageType.SCREEN_CREATED` / `MessageType.SCREEN_CAPTURE`,
      `ScreenCreatedPayload`, `ScreenCapturePayload`, 그리고 `capture_screen` 의
      `returnValue` 를 처음으로 타입으로 적은 `CapturedImage`
- [x] `app/agents/qa/tools.py` — 도구가 그 `returnValue` 를 dict 로 더듬던 자리를
      `CapturedImage` 로 바꾼다. 업로드 경로를 새로 만들지 않는다는 제약이 곧 "읽는 방법도
      하나"라는 뜻이다
- [x] `app/qa/channel.py` — `on_screen_created` (동기 라우팅) + `capture_new_screen`
      (백그라운드), action future 를 `messageId` 로 키잡기, `close()`
- [x] `app/qa/service.py` — `SCREEN_CREATED` 라우팅, 시나리오가 끝날 때 `close()`
- [x] 테스트

## Validation

- `LANGSMITH_TRACING=false uv run pytest`
- 러닝 스택에는 아직 `SCREEN_CREATED` 를 내는 쪽이 없으므로 end-to-end 확인은 ARTEL-456
  뒤로 미룬다. 그 사실을 PR 에 적는다

## Risks & Rollback

- **SDK 가 동시 배치를 어떻게 다루는지 모른다.** 도구의 action 이 떠 있는 동안 자동 capture 가
  또 하나를 민다. JSON-RPC 배치는 각자 correlation 으로 답하므로 이쪽은 섞이지 않지만,
  게임 쪽 직렬화는 확인된 바 없다. 문제가 되면 자동 capture 를 도구 action 과 같은 락 뒤로 옮긴다
- 되돌리기는 frame 두 개를 안 쓰는 것으로 끝난다. `SCREEN_CREATED` 가 오지 않으면 이
  경로는 한 줄도 돌지 않는다
