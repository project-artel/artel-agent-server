# 2026-08-27 — scene view 에 현재 씬의 capability 와 앵커 지식을 넣는다

- Date: 2026-08-27
- Jira: ARTEL-612
- Status: Implemented (전체 테스트 714 통과)
- Base: `develop` (#122·#123 이 머지된 뒤 그 위로 리베이스했다. #125–#128 이 그 사이에
  들어와 `render_now` 를 없앴고, 그래서 블록이 붙는 자리가 바뀌었다 — 아래 "자리를 옮긴
  이유" 참조)

## Goal

런 시작에 orchestration 의 씬 맥락 조회(ARTEL-611)를 **한 번** 부르고, 그 결과를
메모리에 들고, 씬에 들어설 때 씬 뷰 아래에 **그 씬의 조각만** 그린다. agent 가 도구를
부르지 않아도 "여기서 무엇을 할 수 있나" 와 "여기서만 참인 것이 무엇인가" 를 알고
시작한다.

## 자리를 옮긴 이유

원래 계획은 매 모델 호출 뒤에 붙는 `<<current scene>>` 라이브 뷰 **안**이었다. 그
뷰가 없어졌다 — ARTEL-621(#127) 이 걷어냈고, 이유는 그 꼬리가 프롬프트 접두를 매 턴
깨뜨려 캐시가 시스템 프롬프트에서 멈추게 하고 있었다는 것이다. `render_now` 와
`CURRENT_SCENE_START/END` 가 develop 에 더는 없다.

그래서 블록은 화면이 남은 유일한 자리로 간다: 도구 결과가 싣는 씬 뷰
(`SceneMemory.render`) 바로 **아래**, 그리고 씬 뷰 마커 **밖**.

- **아래**인 것은 종전과 같은 이유다. 위가 게임이 지금 하고 있는 것이고, 이것은
  그것을 어디서 하고 있는지에 대한 문서다.
- **마커 밖**인 것은 `fold_stale_scenes` 가 그 마커 쌍 사이를 통째로 자리표로 바꾸기
  때문이다. 안에 넣으면 씬 뷰 하나만 남기는 `fold` 에 블록도 함께 사라진다.
- **턴마다가 아니라 씬마다** 그린다. 도구 결과는 대화에 쌓이므로, 매번 그리면 한 씬에
  머문 턴 수만큼 같은 문단이 컨텍스트에 남는다. 종전 꼬리는 매 호출 교체되는
  메시지라 이 질문이 없었다.
- **압축 원장도 블록을 들고 간다.** ARTEL-622(#128) 가 화면에 대해 세운 보장과 같고,
  이쪽이 더 급하다 — 화면은 다음 도구 결과가 다시 그리지만, 블록을 실은 도구 결과가
  요약으로 대체되면 그 씬에 머무는 동안 다시 오지 않는다.
- **`fold_stale_scene_context` 는 뺐다.** 그것이 있어야 할 이유가 "블록이 매 턴
  쌓이면"이었는데 이제 씬마다 한 번이고, `fold` 는 이미 보낸 메시지를 고쳐 쓰는 일이라
  ARTEL-621 이 없애려던 접두 파괴를 되살린다.
- **v13 이 v12 를 그대로 싣지 않는다.** #127 이 꼬리를 없애면서 프롬프트를 안 고쳤고,
  develop 의 v12 는 아직 없는 `<<current scene>>` 을 가르친다. v13 이 배포 즉시
  기본값이 되므로 그 문장들을 여기서 고친다.

## Non-goals

- `search_knowledge` 에 씬 필터 인자를 붙이지 않는다. 서버 필터는 앵커 없는 지식을
  빼므로 게임 전체 규칙이 오히려 감춰진다.
- `screen_id` 를 agent 에게 보이지 않는다.
- `controlSelectorHint` 를 조준 키로 쓰지 않는다. 액션 프로토콜은 int instance id 를
  받고, selector 는 런마다 밀린다.
- 지식 본문(`description`)을 받아오거나 그리지 않는다. payload 에 없고, 있어도
  블록에 실을 것이 못 된다.
- orchestration 을 건드리지 않는다. ARTEL-611 이 엔드포인트를 이미 열었다.

## Context / Constraints

- **부피가 설계를 정한다.** 이 블록은 씬에 들어설 때 한 번 그려지고, 그 뒤로 그 씬을
  도는 내내 대화에 남는다 — 그 씬의 화면과 같은 자리를 두고 다툰다. capability 8 줄,
  지식 6 줄로 자르고, **자르면 잘랐다고 말한다** — 조용한 절단은 "이게 전부다" 로
  읽힌다.
- **경계를 말한다.** 이 블록에 담기는 것은 이 씬에 앵커된 지식뿐이다. 지식창고의
  대부분인 게임 전체 규칙은 여기 없고 `search_knowledge` 로만 나온다. 눈앞의 목록은
  완전하다는 착각을 부르므로 블록 제목과 프롬프트 한 문장이 그 선을 긋는다.
- **조회 실패는 치명적이지 않다.** 어드바이저리 조회가 실패했다고 런이 시작하지
  못하는 쪽이, 조언 없이 도는 런보다 나쁘다. `fetch` 는 어떤 예외도 밖으로 내지
  않고 `None` 을 준다. 블록만 없다.
- **`project_id` / `game_build_id` 가 아직 오지 않는다.** 지금 orchestration 의
  `QaSessionOpenContext`(`WebSocketQaAgentAdapter.kt`)는 `game_instance_id` 와
  `qa_run_id` 만 보낸다. 두 필드를 `QaContext` 에 **선택**으로 열어 두고, 없으면
  조회 자체를 하지 않는다 — 조회 실패와 같은 자리로 내려앉는다. 보내는 쪽은 별개
  이슈다.
- **`contentMapId: null` 은 정상이다.** `evidence` 를 한 번도 올리지 않은 빌드에서 200 이
  돌아오고 `scenes` 가 비거나 앵커 씬만 있다.
- **`knownToContentMap: false`** 인 씬은 지도가 들어 본 적 없는 씬 이름을 든 앵커에서
  왔다. capability 가 없고 지식은 여전히 유효하다. "이 씬은 아는데 할 게 없다" 와
  "이 씬을 모른다" 는 다른 답이라, 뭉개지 않고 다르게 그린다.
- **`fold` 하지 않는다.** 마커는 달지만 `fold` 하는 함수는 두지 않는다. `fold` 는 이미 보낸
  메시지를 고쳐 쓰는 일이고, 그것이 ARTEL-621 이 잡아낸 접두 파괴의 정체다. 씬마다
  한 번 그리는 문단은 그 값을 치를 만큼 크지 않다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/qa/scene.py`, `app/agents/qa/context.py`,
      `app/agents/qa/runner.py`, `app/qa/service.py`, `app/api/qa_sessions.py`,
      ARTEL-611 의 `SceneContextDtos.kt`.
- [x] **Step 1: `app/qa/scene_context.py` 신설**
  - ARTEL-611 payload 의 타입 모델 (`SceneContext`, `SceneContextEntry`,
    `SceneCapability`, `SceneKnowledge`). id 계열은 전부 문자열, camelCase alias.
  - `MAX_CAPABILITIES_IN_SCENE_CONTEXT = 8`, `MAX_KNOWLEDGE_IN_SCENE_CONTEXT = 6`,
    `MAX_TEXT_CHARS = 160`. 왜 이 숫자인지 주석이 `MAX_ACTIONS_IN_LIVE_VIEW` 를
    가리킨다.
  - `SCENE_CONTEXT_START` / `SCENE_CONTEXT_END` 마커.
  - `SceneContext.render(scene_name) -> str | None` — 항목이 없으면 `None`.
  - `fetch_scene_context(...) -> SceneContext | None` — 예외를 밖으로 내지 않는다.
- [x] **Step 2: `app/qa/scene.py`** — `SceneMemory` 가 `scene_context` 를 필드로
      들고, `render` 가 씬이 바뀐 뒤 첫 렌더에만 씬 뷰 마커 **밖**에 블록을 붙인다.
      여기서 조회는 하지 않는다 — 담기만 한다.
- [x] **Step 3: `app/agents/qa/compaction.py`** — 원장이 화면과 함께 블록도 싣는다.
      `render` 가 방금 그렸으면 두 번 말하지 않는다.
- [x] **Step 4: `app/agents/qa/runner.py`** — 손대지 않는다. 화면을 그리는 자리가
      `SceneMemory` 하나뿐이라 러너를 거칠 이유가 없다.
- [x] **Step 5: 배관** — `QaContext` / `QaSessionRecord` 에 `project_id`,
      `game_build_id` 선택 필드. `QaExecutionService.run` 이 시나리오마다 한 번
      조회해 `channel.scene.scene_context` 에 얹는다 (지식 스코프가 `qa_try` 단위라
      시나리오당이다).
- [x] **Step 6: `app/prompts/qa_run/v13/`** — 블록을 읽는 법과 경계를 더하고, v12 가
      아직 없는 `<<current scene>>` 꼬리를 가르치던 네 문단을 고친다.
      `python -m app.prompts.lock --write`.
- [x] **Step 7: 테스트** — `tests/test_qa_scene_context.py` 신설,
      `tests/test_qa_prompt_version.py` 의 기본 버전 v13 으로.
- [x] **Step 8: 전체 diff 검토 후 커밋.**

## Test Plan

`env -u OPENROUTER_API_KEY uv run --extra dev pytest` (develop baseline 683 collected,
이 브랜치 714 passed).

- capability 와 지식이 둘 다 있는 씬의 블록
- 지도는 아는데 둘 다 없는 씬 ("아는데 할 게 없다")
- 지도가 모르는 씬 (`knownToContentMap=false`) — capability 없음, 지식은 나온다
- payload 에 아예 없는 씬 — 블록 자체가 없다
- 상한 초과 — 잘리고, 몇 개가 잘렸는지 말한다
- 조회 실패(HTTP 오류·타임아웃·깨진 payload) — `None`, 런은 그대로 돈다
- 씬이 바뀌면 블록 내용이 바뀐다 (러너 실제 구동)
- 블록이 씬 방문당 한 번만 그려진다
- 씬 뷰가 `fold` 돼도 블록은 남는다 (마커 밖이라는 것의 실제 결과)
- 압축 원장이 블록을 들고 가고, 두 번 말하지 않는다

## Risks

- **`project_id` / `game_build_id` 를 보내는 쪽이 아직 없다.** 그때까지 이 기능은
  프로덕션에서 조용히 꺼져 있다. 배포해도 회귀는 없지만, 켜지려면 orchestration 이
  두 필드를 실어야 한다.
- 새 프롬프트 버전은 `resolve_version` 이 가장 높은 번호를 고르므로 v13 을 만드는
  순간 고정하지 않은 모든 런이 v13 을 읽는다. 그래서 블록을 그리는 코드와 같은
  변경에 실린다.
