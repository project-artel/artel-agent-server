# 플레이가 배운 기능을 지도에 적는다 (ARTEL-645)

## Why

`artel_integration` 실측: `capability` 472 행 중 `verification = 'confirmed'` 이 2 행이다.
472 행 중 418 행이 `interaction = 'none'` 이라 action 전후의 `pulse` 를 비교하는 기계 검증으로는
영영 볼 수 없고(ARTEL-450 이 백로그로 내려간 이유), 일어났는지는 화면을 본 agent 가 안다.

양쪽 반쪽이 이미 있다.

- orchestration 이 `CAPABILITY_VERDICT` · `CAPABILITY_DISCOVERED` 를 받는다 (ARTEL-644, V71).
  계약은 `docs/capability-write-frames.md` 와 `contentmap/observe/CapabilityWriteFrames.kt` 다.
- `/internal/scene-context` 가 `capabilities` 와 `notAStepCapabilities` 두 칸으로 낸다
  (ARTEL-680, PR 225).

이 작업은 **쓰는 쪽**이다. 이것이 없으면 그 사슬이 아무것도 돌지 않는다.

## What

1. `app/qa/envelope.py` — 인입 타입 셋과 payload 모델 넷. 철자는 Kotlin 이 정한 그대로.
2. `app/qa/channel.py` — `write_capability` 와 `on_capability_write_result`.
   답의 `type` 을 요청과 대조한다(`on_knowledge_write_result` 와 같은 검사).
3. `app/qa/service.py` — `CAPABILITY_WRITE_RESULT` 를 채널에 넘긴다.
4. `app/agents/qa/capability.py` — tool 설명 셋과 결과 문장. `knowledge.py` · `screen.py` 와 같은 자리.
5. `app/agents/qa/tools.py` — tool 셋.
   - `record_capability_verdict` — 지도에 이미 있는 행에 대한 판단
   - `record_new_capability` — 근거에 없던 것
   - `list_scene_capabilities` — 지금 씬의 capability 를 찾아 키를 준다
6. `app/qa/scene_context.py` — `notAStepCapabilities` 를 읽고 블록에 그린다.
7. `app/prompts/qa_run/v15` — v14 + 문단 하나 + 절 하나. 잠금 파일 재생성.

## 무엇을 agent 에게 보이나

실측 씬별 분포(`artel_integration`, `merged_into IS NULL`):

| scene | 누를 수 있는 것 | `not-a-step` |
|---|---|---|
| TurnBattleScene | 8 | 224 |
| DontDestroyOnLoad | 0 | 64 |
| EndingScene | 2 | 46 |
| StoryScene | 2 | 46 |
| Map_scene | 16 | 30 |
| TitleScene | 16 | 2 |
| GameClearScene | 8 | 4 |
| GameOverScene | 1 | 2 |
| BattleScene | 1 | 0 |

씬 문맥 블록은 씬에 처음 들어갈 때 한 번 그려지고 런이 끝날 때까지 문맥에 남는다.
TurnBattleScene 의 232 행을 다 그리면 한 줄이 약 150자이므로 35KB 가 그 런 내내 앉아 있고,
그것이 판정을 읽어 낼 화면과 자리를 다툰다. 그래서 **블록에는 맛보기만 그리고
(누를 수 있는 것 8 줄 + `not-a-step` 6 줄), 나머지는 tool 로 당겨 온다.**

`list_scene_capabilities` 를 두는 이유가 그것이다. agent 가 실제로 하는 일은 224 행을
훑는 것이 아니라 **방금 본 것에 해당하는 행을 찾는 것**이라, 페이지 넘기기보다 `contains`
검색이 그 일에 맞고 문맥도 훨씬 덜 쓴다. 페이지도 함께 낸다 — 검색어를 못 고를 때가 있다.

## 거절 경로

계약이 거절하는 것을 tool 이 먼저 거절한다. 왕복 하나를 아끼는 것보다, 거절 사유가
"무엇을 고치면 되는지" 를 그 자리에서 말할 수 있는 것이 크다.

- `inferred` 인데 `based_on` 이 비면 거절 — 이슈가 이름을 댄 경우다
- `based_on` 이 이 런이 받은 적 없는 observation id 를 대면 거절. 이 런이 받은 id 는
  `CAPABILITY_WRITE_RESULT.observation_id` 뿐이고, `knowledge_seen` 과 같은 장치다
- `observed` 인데 verdict 가 없으면 거절, `inferred` 인데 verdict 가 있으면 거절
- `capability_key` 와 `capability_id` 가 둘 다 있거나 둘 다 없으면 거절
- `interaction = press` 와 `input_key` 가 짝이 안 맞으면 거절
- 씬 이름을 아직 모르면 거절

## 안 하는 것

- `capture_id` · `screen_id` 를 안 싣는다. 계약에서 선택이고, 지금 붙일 수 있는 값은
  "이 런의 마지막 capture" 뿐이라 판정한 순간의 것이라는 보장이 없다. 근거가 아닌 값을
  근거 칸에 넣는 것이 빈칸보다 나쁘다
- `action.attempts` 를 안 싣는다. 이 런의 dispatch 중 무엇이 이 capability 의 재시도였는지
  가릴 방법이 없다. 서버 기본값 1 로 둔다
- 씬과 화면은 agent 가 안 적는다. 그쪽은 관측이 파생한다
