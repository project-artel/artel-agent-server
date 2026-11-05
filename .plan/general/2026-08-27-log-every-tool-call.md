# 2026-08-27 — 모든 tool 호출과 그 결과를 타임라인에 낸다

- Date: 2026-08-27
- GitHub Issue: None (Jira: ARTEL-609, 스토리 ARTEL-615, 에픽 ARTEL-607)
- Status: Draft

## Goal

모델이 부른 tool 마다 `TOOL` 프레임을, 그 tool 이 답할 때마다 `TOOL_RESULT` 프레임을 낸다.
tool 이 각자 남기던 `thought` 산문 한 줄은 없앤다 — 그 값은 호출 프레임의 `args` 안에 이미
있다.

## Non-goals

- `ACTION` / `ACTION_RESULT` 프레임 변경. 그것은 조작 tool 이 SDK 로 내보낸 요청이다.
- tool 구현 자체의 동작 변경. 로그만 바뀐다.
- 모델의 순수 추론(`thinking` 블록) 처리 변경. 지금처럼 `LOG` THOUGHT 로 남는다.

## Context / Constraints

`_log_reasoning` 이 유일한 길목이다. `stream_mode="updates"` 로 흐르는 모든 노드 업데이트가
여기를 지나고, 모델 턴의 `message.tool_calls` 에 이름·인자·id 가, ToolMessage 에 `name`·
`tool_call_id`·`content` 가 이미 실려 있다. 지금은 둘 다 stdout 로만 나간다. 여기서 내면
tool 28개가 한 번에 덮이고 새 tool 이 생겨도 저절로 따라온다.

`_TURN_PRODUCING_NODES` 게이트를 그대로 둔다. 컴팩션 미들웨어가 다시 쓴 메시지 목록을 통째로
보고하므로, 그 게이트가 없으면 같은 호출이 컴팩션마다 한 번씩 더 실린다.

correlation 은 채널이 든다. 짝을 맺는 `tool_call_id` 는 모델이 지은 값이고 프레임의
messageId 는 채널이 지은 값이라, 그 대응을 아는 자리가 채널 말고 없다. 러너에 두지 않은
것은 러너 인스턴스가 런 하나만 사는지 보장되지 않기 때문이다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `_log_reasoning` 호출 지점, `_TURN_PRODUCING_NODES`, `_clip`,
      tools.py 의 `channel.note` 자리 확인.
- [x] **Step 1: 프레임 계약** — `MessageType.TOOL` / `TOOL_RESULT`, `ToolCallPayload` /
      `ToolResultPayload`. `message` 에 tool 이름을 싣는 이유(Orchestration 의 non-blank
      가드)를 모델 옆에 적는다.
- [x] **Step 2: 채널** — `tool_call` / `tool_result`, 그리고 `tool_call_id -> messageId`
      대응.
- [x] **Step 3: 러너** — `_log_tool_call` / `_log_tool_result` / `_step_of`. 결과 본문은
      콘솔과 같은 문자열을 쓰도록 부르는 쪽에서 한 번만 자른다.
- [x] **Step 4: 산문 제거** — tools.py 의 `channel.note(thought, THOUGHT, step)` 16곳 삭제.
      `_run` 의 `thought` 인자는 그 note 가 유일한 쓰임이었으므로 함께 뺀다(호출 14곳).
      `capture_screen` 의 캡처 URL OBSERVATION 줄은 남긴다 — 이미지 링크가 거기에만 있다.
- [x] **Step 5: 테스트** — 호출 프레임의 필드, correlation, 짝 없는 결과, 이름·id 없는 호출,
      긴 결과 자르기.

## Validation

- **Commands to run:** `LANGSMITH_TRACING=false .venv/bin/python -m pytest -q`
- **Expected output:** 전부 통과. `tests/test_qa_reasoning_log.py` 15개 포함.
- **Note:** 트레이싱을 끄지 않으면 LangSmith 월 한도 초과 429 로그가 출력을 덮어 결과가
  안 보인다.

## Risks & Rollback

- **Risks:**
  - Orchestration 이 `TOOL` 을 아직 모르면 프레임이 통째로 거절된다. ARTEL-608 이 먼저
    머지돼야 하는 이유다.
  - 프레임 수가 늘어난다. 런 하나가 tool 호출 수만큼 행을 더 만든다. 대신 tool 이 내던
    THOUGHT 줄이 같은 수만큼 사라지므로 총량은 결과 프레임만큼만 는다.
  - `thought` 를 지운 자리를 화면이 아직 안 읽으면, home 이 머지되기 전까지 그 이유가
    타임라인에서 잠깐 보이지 않는다. `args` 에는 실려 있으므로 유실은 아니다.
- **Rollback steps:** `git revert` 한 커밋.

## Open Questions

- 없음.
