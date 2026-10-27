# Evaluator 가 싣는 frame — step `STATUS` 의 `analysis`·`played`, 종단 `STATUS` 의 런 리뷰

- planner–executer 엔진(`engine="planner_executer"`)의 QA 런이 orchestration 에 보내는 `STATUS` 에 더해진 필드의 계약
- 보내는 쪽은 `app/agents/evaluator/`(ARTEL-1267). orchestration 은 이 필드를 **읽지 않고** `qa_log.payload` 에 통째로
  저장한다. 읽는 쪽은 그 뒤에 생기는 화면과 승인 흐름이다
- `MessageType` 은 늘지 않았다. 모르는 타입은 orchestration 이 frame 째 버리므로(`qa-protocol.md` 함정 2), 있는 frame 의
  필드로만 싣는다
- 모양이 바뀌면 이 문서를 같은 PR 에서 고친다. 읽는 쪽이 이 문서를 보고 짠다

## 한눈에

| frame | 언제 | 더해진 필드 | 누가 쓰나 |
| --- | --- | --- | --- |
| step `STATUS` | step 하나가 판정될 때 | `analysis`(실패한 step 만), `played`(play 한 step 만) | 사람이 보는 case 결과 칸, 영상 클립 |
| 종단 `STATUS` | 런이 정상 종료할 때 | `analysis`(런 리뷰) | 런 요약 |
| `LOG` | 종단 `STATUS` 직전 | `kind: "run_review"`, `decision: {patterns, insights}` | 타임라인 |

- `ISSUE` 는 보내지 않는다. 실패한 case 를 이슈로 옮길지는 사람이 그 판정을 승인하며 정하고, 그때 orchestration 이
  `analysis` 의 내용(`title`·`severity`·`cause`·`reproduction`)과 클립으로 이슈를 만든다. tool loop 엔진의
  `report_issue` 는 그대로 `ISSUE` 를 보낸다
- 필드가 없을 때는 key 자체가 없다. `null` 을 보내지 않는다. 그래서 `analysis="off"` 런과 tool loop 런의 `STATUS` 는
  이 필드가 생기기 전과 byte 단위로 같다

## step `STATUS`

기존 필드(`status`·`step`·`case_id`·`is_verification`·`message`)는 그대로다. `message` 가 판정 이유다 — 통과한 step 의
"왜 성공으로 봤나" 는 여기에 있고, 실패한 step 은 `analysis` 가 더 말한다.

```json
{
  "status": "FAILED",
  "step": 1,
  "case_id": 5,
  "is_verification": true,
  "message": "The menu is still shown.",
  "played": {
    "start_frame": 101,
    "end_frame": 112,
    "started_at": "2026-10-27T06:41:03Z",
    "ended_at": "2026-10-27T06:41:09Z",
    "seconds": 6.2,
    "stopped": "done"
  },
  "analysis": {
    "category": "GAME_DEFECT",
    "cause": "Start 를 눌렀고 게임이 받았는데 메뉴가 그대로다.",
    "reproduction": ["타이틀 화면에서 Start 버튼을 누른다."],
    "reproduction_inputs": [
      {
        "n": 1,
        "scene": "Menu",
        "kind": "click",
        "target_text": "Start",
        "target_name": "Start",
        "to_text": null,
        "key": null,
        "value": null,
        "result": "ok",
        "source": "click(start)"
      }
    ],
    "actual": "메뉴가 그대로 보인다.",
    "insight": "Start 버튼의 handler 가 무엇을 하는지 본다.",
    "engine_claimed": true,
    "title": "Start 가 지도를 열지 않는다",
    "severity": "MAJOR"
  }
}
```

값은 `tests/test_evaluator_analysis.py` 의 fixture 다. 실제 런이 아니다.

### `played` — 영상 클립을 자를 기준

| key | 타입 | 뜻 |
| --- | --- | --- |
| `start_frame` | int \| null | play 가 시작할 때 `pulse` 의 마지막 reading 이 말한 SDK frame 번호 |
| `end_frame` | int \| null | play 가 멈출 때의 같은 값 |
| `started_at` | string | play 시작 시각, UTC ISO 8601 초 단위(`…Z`). envelope `timestamp` 와 같은 눈금 |
| `ended_at` | string | play 가 멈춘 시각 |
| `seconds` | number | 둘의 차. 소수 첫째 자리 |
| `stopped` | string | play 가 멈춘 이유. `done` 이면 `done()` 이 참이고 Jev 도 동의한 것 |

- **판정은 frame 이 아니다.** 판정은 play 가 멈춘 뒤 모델이 답한 시각이라 영상에 그 순간이 없다. step 의 클립은
  `[start_frame, end_frame]` 에 앞뒤 여유를 붙여 자른다. 판정 이유가 가리키는 화면은 `end_frame` 근처다
- reading 이 아직 없으면(`pulse` 가 한 번도 안 왔으면) frame 은 `null` 이고 시각만 있다
- **play 한 step 에만 있다.** `PLANNER-REFUSED` 로 play 없이 실패한 step, FREE 에서 씬에 닿지 못해 play 없이
  `UNREACHED` 가 된 case 에는 key 가 없다. FREE 에서 play 했는데 목표에 닿지 못한 case 는 마지막 play 의 값이다
- FREE 는 case 마다 play 가 따로라 case 마다 구간이 따로 나온다

### `analysis` — 실패한 step 의 분석

| key | 타입 | 뜻 |
| --- | --- | --- |
| `category` | string | `GAME_DEFECT` · `UNREACHED` · `INCONCLUSIVE` · `AGENT_ERROR` |
| `cause` | string | 원인. 증거(화면의 값·글자, 거절 사유)를 이름으로 가리킨다. 2000자 상한 |
| `reproduction` | string[] | 사람이 따라 할 문장, 순서대로. 런의 언어(`RunConfig.language`). 문장당 300자 |
| `reproduction_inputs` | object[] | 문장마다 그 소스. 아래 표. `reproduction[i]` 와 `reproduction_inputs[i]` 가 짝이다 |
| `actual` | string | 기대 결과 대신 게임이 보인 것 |
| `insight` | string | 먼저 볼 곳 한두 문장 |
| `engine_claimed` | bool | 엔진이 목표에 닿았다고 봤는지(`done()` 이 어느 turn 에서 참) |
| `title` | string | `GAME_DEFECT` 에만. 이슈 초안의 한 줄 제목 |
| `severity` | string | `GAME_DEFECT` 에만. `BLOCKER` · `CRITICAL` · `MAJOR` · `MINOR` · `TRIVIAL` |

분류의 뜻:

| `category` | 뜻 | 이슈 후보인가 |
| --- | --- | --- |
| `GAME_DEFECT` | 입력이 게임에 닿았고 화면이 기대와 다른 것을 보였다. 모델의 추천이고 사람이 정한다 | 예 |
| `UNREACHED` | play 가 step 이 확인할 상태에 닿지 못했다. 게임을 시험하지 못한 것이다 | 아니오 |
| `INCONCLUSIVE` | 화면이 결과를 어느 쪽으로도 보여 주지 못했다 | 아니오 |
| `AGENT_ERROR` | 엔진이 스스로 멈췼다(`PLANNER-REFUSED` · `JEV-ERROR` · `EVALUATOR-ERROR`), 또는 분석 답을 두 번 읽지 못했다(`ANALYSIS-ERROR`) | 아니오 |

- `UNREACHED` 와 엔진이 멈춘 `AGENT_ERROR` 는 모델을 부르지 않고 코드가 채운다. `reproduction` 은 비고
  `reproduction_inputs` 에는 보낸 입력이 전부 실린다
- 미승인(오탐·미탐) 로그를 남길 때 `engine_claimed`(엔진 주장) · 판정(`status`) · 사람의 결정 셋이 confusion matrix 의
  세 축이다

#### `reproduction` 과 `reproduction_inputs` 의 관계

- 문장은 **모델이** 런의 언어로 쓴다. 소스는 **코드가** 만든다 — play 가 게임에 보낸 입력마다 사람이 본 값을 기록한다
- 모델은 입력 번호마다 한 문장을 쓰고, 코드가 그 문장을 소스에 묶는다. 번호가 범위 안이고 중복이 없고, selector 경로 ·
  `(Clone)` · `[n]` · `click(` 같은 코드 표기가 없고, **그 입력이 닿은 객체의 화면 글자(드래그는 양끝, 키는 키 이름)가
  문장에 그대로** 있어야 통과한다. 걸리면 한 번 더 묻고 두 번째도 걸리면 `AGENT_ERROR` 다
- 그래서 `reproduction[i]` 를 읽고 미심쩍으면 `reproduction_inputs[i]` 를 본다. 글자가 없는 객체(스프라이트)는 소스가
  `target_name` 까지만 말해 주고 문장 검사가 걸리지 않는다 — 그 자리는 클립이 메꾼다

`reproduction_inputs[i]`:

| key | 타입 | 뜻 |
| --- | --- | --- |
| `n` | int | 입력 번호. play 가 보낸 순서, 1 부터 |
| `scene` | string \| null | 그 입력을 보낼 때의 씬 |
| `kind` | string | `click` · `double_click` · `drag` · `move` · `key` · `key_down` · `key_up` · `text` · `hold` · `release` · `axis` · `button` · `time` · `other` |
| `target_text` | string \| null | 닿은(없으면 겨눈) 객체가 화면에 띄운 글자. 문장이 담아야 하는 값 |
| `target_name` | string \| null | 그 객체의 이름(`(Clone)[n]` 뗀 것). 글자가 없을 때의 이름표 |
| `to_text` | string \| null | 드래그의 도착 객체 글자(없으면 이름) |
| `key` | string \| null | `key*` 의 KeyCode 이름 |
| `value` | string \| null | `text` 가 입력한 글 |
| `result` | string | `ok` · `refused: <사유>` · `landed on nothing` · `blocked by <경로>` |
| `source` | string | macro runner 가 보낸 원문 줄(`click(start)`). 디버깅용. 사람이 따라 할 글이 아니다 |

## 종단 `STATUS` 의 `analysis` — 런 리뷰

정상 종료한 런에만 있다. 취소·시간 초과로 끊긴 런(`service._send_terminal`)에는 없다.

```json
{
  "status": "COMPLETED",
  "result": "FAILED",
  "message": "3/5 steps passed.",
  "summary": { "steps": { "...": "기존 그대로" }, "cases": { "...": "기존 그대로" } },
  "analysis": {
    "overview": "5 step 중 3 개가 통과했다. 실패 둘은 모두 전투 씬에서 카드를 끌어다 놓는 입력이 거절된 것이고, 게임보다 테스트 케이스의 사전조건 쪽 문제로 보인다.",
    "patterns": ["step 2, 4 가 같은 거절 사유 `Nothing at selector: Hand/Card[0]` 로 멈췼다."],
    "insights": ["전투 진입 직후 손패가 비는지 확인하고, 카드가 있을 때 끄는 사전조건을 케이스에 적는다."]
  }
}
```

| key | 타입 | 뜻 |
| --- | --- | --- |
| `overview` | string | 두 네 문장. 2000자 상한 |
| `patterns` | string[] | step 을 가로질러 반복된 것. 최대 10 개, 각 500자 |
| `insights` | string[] | 먼저 할 일, 효과 큰 순서. 최대 10 개, 각 500자 |

같은 `overview` 가 종단 `STATUS` 직전에 `LOG` 한 줄로도 나간다 — `category: "THOUGHT"`, `kind: "run_review"`,
`message: <overview>`, `decision: {"patterns": [...], "insights": [...]}`. 타임라인에서 바로 읽히게 하려는 것이다. 리뷰 답을
두 번 읽지 못하면 둘 다 없고 `summary` 만 간다.

## 켜고 끄기

- `arch.planner.analysis` — `"on"`(기본) · `"off"`. `off` 면 `analysis` 도 런 리뷰도 없고 `played` 만 남는다
- `on` 은 prompt `qa_planner` v4 이상을 요구한다. 그보다 옛 version 을 고정한 요청은 `QaArchError` 로 거절된다 —
  조용히 끄면 `analysis="on"` 으로 기록된 런이 분석 없이 돈다
- 언어는 `RunConfig.language`(`ko` · `en`). 분석과 리뷰의 자연어가 그 언어로 나오고, 화면 글자와 키 이름은 번역하지 않는다
- 런 기록: label `pe3-evaluator-analysis`, `run_config.prompt_version` v4. `fingerprint` 는 knob 이 늘어 pe2 와 다르다

## 받는 쪽이 할 것

- `ISSUE_RESULT` 는 agent-server 가 받는다(`deliver()`). tool loop 가 보낸 `ISSUE` 의 회신이고 이제 「unsupported inbound
  frame」으로 되돌아가지 않는다
- step `STATUS` 의 `analysis`·`played` 는 `qa_log.payload` 에 저장만 되고 어느 컬럼으로도 승격되지 않았다. 화면이
  읽으려면 그 자리를 orchestration 이 정한다
- 1 MiB frame 상한 안이다. `cause`·`actual`·`insight` 2000자, 문장 300자 × 30, 리뷰 목록 10 × 500자
