# 2026-07-28 — QA 에이전트가 캡처한 화면을 보고 판정한다

- Date: 2026-07-28
- Jira: ARTEL-143
- Status: Implemented

## Goal

QA 에이전트가 씬 텍스트로 표현되지 않는 결함 — 깨진 레이아웃, 다른 UI에 가린 버튼,
잘못된 상태의 스프라이트, 읽히지 않는 글자 — 을 실제 화면을 보고 판정한다.

## Non-goals

- artel-home 타임라인에 캡처 표시(별도 이슈)
- 캡처 생성·업로드(SDK, ARTEL-141), 서명·증거 적재(오케스트레이션, ARTEL-142)
- WebRTC 라이브 스트림 변경
- 스텝마다 자동 스크린샷

## Context / Constraints

**이미지는 QA WebSocket을 타지 않는다.** 오케스트레이션이 중계 프레임 전체를
`qa_log.payload`에 적재하고 SSE로 다시 발행한다. 에이전트는 URL만 받고 바이트는 HTTP로
직접 받는다.

**이슈가 적은 것보다 제약이 강했다.** 이슈는 "OpenAI chat/completions는 tool 롤 메시지에
이미지를 허용하지 않아 OpenAI 모델에서 깨진다"라고 적었지만, `build_chat_model`은 모든
모델을 `ChatOpenAI` + OpenRouter로 만든다 — `anthropic/claude-sonnet-5`를 포함해서. 즉
이건 특정 프로바이더의 엣지 케이스가 아니라 **모든 경로**의 제약이고, 별도 `HumanMessage`
주입은 우회책이 아니라 유일한 길이다.

**langchain 1.3.14 / langchain-core 1.5.1 고정.** `create_agent(middleware=[...])`,
`AgentMiddleware.abefore_model` / `awrap_model_call`(`request.override(...)`).

## 설계 결정

**이미지는 툴 결과가 아니라 별도 `HumanMessage`로 주입한다.** `abefore_model`에서 넣으면
그 턴의 `ToolMessage`가 전부 이미지보다 앞에 오는 것도 함께 보장된다(Anthropic이 요구하는
순서). 툴은 URL을 상태에 쌓아두기만 한다.

**요청에 실리는 이미지를 최근 2장으로 제한한다.** 없으면 지금까지의 모든 스크린샷이 매 턴
재전송되어 런 비용이 캡처 수의 제곱으로 는다. 오래된 것은 자리를 지키되 그림만 빠지고
"비용 때문에 뺐다, 필요하면 다시 찍어라"라는 문장으로 바뀐다. 에이전트는 지금 보고 있는
것으로 판정하고, 예전 화면의 결론은 이미 대화에 남아 있다.

**URL을 넘기지 않고 우리가 받아서 base64로 싣는다.** 링크는 만료되고, 스토리지가 공개
읽기가 아닐 수 있으며, 우리 쪽 실패는 프로바이더 오류가 아니라 에이전트에게 설명할 수
있는 실패가 된다.

**실패는 전부 런을 이어간다.** URL 만료·타임아웃·`returnValue` 누락·게임 무응답 모두
읽을 수 있는 문장으로 돌려주고, 다시 찍을지 씬 텍스트로 판정할지는 에이전트가 정한다.

로컬 실런에서 이 원칙이 한 분기에서 지켜지지 않은 것이 드러났다. 액션 자체가 거절된
경우(`success=false`)만 이유를 말하고 대안을 주지 않았는데, 이 액션을 모르는 구버전 SDK가
붙으면 매 캡처가 그 분기로 떨어진다. 에이전트는 스텝을 실패시키고 런까지 종료했다 —
없어도 되는 스크린샷 한 장 때문에. 이제 "씬 텍스트로 판정하라"를 함께 돌려준다.

같은 런에서 런당 상한이 실패를 세지 않는 것도 드러났다. 모든 캡처가 거절되는 게임에서는
상한에 영원히 걸리지 않아 왕복만 반복된다. 성공이 아니라 **시도**를 센다.

**`supports_vision=false` 모델에는 툴을 아예 주지 않는다.** 남겨두면 호출되고, 게임
왕복을 쓰고, 아무도 볼 수 없는 이미지를 만든다. 게다가 에이전트는 왜 봐도 소용이 없는지
알 방법이 없다. 시스템 프롬프트의 비전 지시문도 같이 빠진다.

**런당 캡처 상한 12장.** 판정 대신 계속 보기만 하는 런은 데드라인에 아무것도 보고하지
못한 채 도달한다. 초과 시 이유를 밝히며 거절한다.

**캡처 URL을 `OBSERVATION` 로그로 남긴다.** 리뷰어가 에이전트가 본 것을 정확히 열 수 있다.

## 변경 목록

- `app/agents/qa/vision.py` — 이미지 가져오기, 주입 미들웨어, 이미지 상한
- `app/agents/qa/tools.py` — `capture_screen` 툴, `PendingCapture`, 런당 상한
- `app/agents/qa/runner.py` — 미들웨어 배선, 비전 지시문, 모델별 분기
- `app/llm/models.py` — `ModelSpec.supports_vision`, 카탈로그 노출
- `app/qa/envelope.py` — `ActionResultItem.returnValue`

## Validation

- `python -m pytest` — 118건 전부 통과(기존 99 + 신규 19).
- 실제 모델 런(`gpt-4o-mini`, `claude-sonnet-5`)으로 그림에만 보이는 근거가 타임라인에
  인용되는지는 확인하지 않았다. 살아 있는 게임과 OpenRouter 자격증명이 필요하고, 아직
  SDK 쪽 픽셀 경로(ARTEL-141)의 플레이모드 검증도 남아 있다. 세 저장소가 붙은 뒤
  엔드투엔드로 확인해야 한다.
- LangSmith 트레이싱이 켜져 있으면 base64 이미지가 트레이스에 남는다. 그대로 둔다.
