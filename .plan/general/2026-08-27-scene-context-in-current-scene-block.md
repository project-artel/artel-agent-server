# 2026-08-27 — scene view 에 현재 씬의 capability 와 앵커 지식을 넣는다

- Date: 2026-08-27
- Jira: ARTEL-612
- Status: Implemented (전체 테스트 707 통과, PR 미개설)
- Base: `feat/agent-record-knowledge-에-앵커를-싣고-검색-결과에-보인다-ARTEL-592` (ARTEL-590 위에 쌓인 스택)

## Goal

런 시작에 orchestration 의 씬 맥락 조회(ARTEL-611)를 **한 번** 부르고, 그 결과를
메모리에 들고, 매 모델 호출마다 `<<current scene>>` 블록 안에 **현재 씬의 조각만**
그린다. agent 가 도구를 부르지 않아도 "여기서 무엇을 할 수 있나" 와 "여기서만 참인
것이 무엇인가" 를 알고 시작한다.

## Non-goals

- `search_knowledge` 에 씬 필터 인자를 붙이지 않는다. 서버 필터는 앵커 없는 지식을
  빼므로 게임 전체 규칙이 오히려 감춰진다.
- `screen_id` 를 agent 에게 보이지 않는다.
- `controlSelectorHint` 를 조준 키로 쓰지 않는다. 액션 프로토콜은 int instance id 를
  받고, selector 는 런마다 밀린다.
- 지식 본문(`description`)을 받아오거나 그리지 않는다. payload 에 없고, 있어도
  매 턴 다시 그릴 것이 못 된다.
- orchestration 을 건드리지 않는다. ARTEL-611 이 엔드포인트를 이미 열었다.

## Context / Constraints

- **부피가 설계를 정한다.** 이 블록은 관측마다가 아니라 **모델 호출마다** 다시
  쓰인다. `app/qa/scene.py` 의 `MAX_ACTIONS_IN_LIVE_VIEW` 가 `MAX_ACTIONS`(40) 이
  아니라 10 인 이유가 그대로 적용된다. capability 8 줄, 지식 6 줄로 자르고, **자르면
  잘랐다고 말한다** — 조용한 절단은 "이게 전부다" 로 읽힌다.
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
- **`contentMapId: null` 은 정상이다.** 근거를 한 번도 올리지 않은 빌드에서 200 이
  돌아오고 `scenes` 가 비거나 앵커 씬만 있다.
- **`knownToContentMap: false`** 인 씬은 지도가 들어 본 적 없는 씬 이름을 든 앵커에서
  왔다. capability 가 없고 지식은 여전히 유효하다. "이 씬은 아는데 할 게 없다" 와
  "이 씬을 모른다" 는 다른 답이라, 뭉개지 않고 다르게 그린다.
- **접기.** `fold_stale_scenes` · `fold_stale_knowledge` 와 같은 방식으로 마커를 달고
  접는 함수를 둔다. 오늘 `<<current scene>>` 은 `request.override` 로 매 호출 새로
  붙고 그래프 상태에 남지 않으므로 실제로 쌓이지 않는다 — 이 접기는 **그 사실이
  바뀌는 날을 위한 가드**이고, 주석과 테스트가 그렇게 말한다.

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
- [x] **Step 2: `app/qa/scene.py`** — `render_now(context: str | None = None)` 가
      받은 블록을 끝 마커 앞에 붙인다. `SceneMemory` 는 payload 를 모른다.
- [x] **Step 3: `app/agents/qa/context.py`** — `fold_stale_scene_context`.
      `ToolMessage` 만 보는 두 형제와 달리 **모든 메시지**를 본다 (블록이
      `HumanMessage` 를 타므로).
- [x] **Step 4: `app/agents/qa/runner.py`** — `run` / `run_with_deadline` 가
      `scene_context` 를 받고, `_build_append_current_scene` 이 블록을 그린다.
      `_fold_scene_views` 가 새 접기도 부른다 (미들웨어 **이름** 목록은 그대로 —
      arch fingerprint 를 흔들지 않는다).
- [x] **Step 5: 배관** — `QaContext` / `QaSessionRecord` 에 `project_id`,
      `game_build_id` 선택 필드. `QaExecutionService.run` 이 시나리오마다 한 번
      조회해 러너에 넘긴다 (지식 스코프가 `qa_try` 단위라 시나리오당이다).
- [x] **Step 6: `app/prompts/qa_run/v13/`** — v12 문장을 그대로 두고 블록을 읽는
      법과 경계 한 문장을 더한다. `python -m app.prompts.lock --write`.
- [x] **Step 7: 테스트** — `tests/test_qa_scene_context.py` 신설,
      `tests/test_qa_prompt_version.py` 의 기본 버전 v13 으로.
- [x] **Step 8: 전체 diff 검토 후 커밋.**

## Test Plan

`env -u OPENROUTER_API_KEY uv run --extra dev pytest` (baseline 681 passed).

- capability 와 지식이 둘 다 있는 씬의 블록
- 지도는 아는데 둘 다 없는 씬 ("아는데 할 게 없다")
- 지도가 모르는 씬 (`knownToContentMap=false`) — capability 없음, 지식은 나온다
- payload 에 아예 없는 씬 — 블록 자체가 없다
- 상한 초과 — 잘리고, 몇 개가 잘렸는지 말한다
- 조회 실패(HTTP 오류·타임아웃·깨진 payload) — `None`, 런은 그대로 돈다
- 씬이 바뀌면 블록 내용이 바뀐다 (러너 실제 구동)
- `fold_stale_scene_context` 가 오래된 블록만 접는다

## Risks

- **`project_id` / `game_build_id` 를 보내는 쪽이 아직 없다.** 그때까지 이 기능은
  프로덕션에서 조용히 꺼져 있다. 배포해도 회귀는 없지만, 켜지려면 orchestration 이
  두 필드를 실어야 한다.
- 새 프롬프트 버전은 `resolve_version` 이 가장 높은 번호를 고르므로 v13 을 만드는
  순간 고정하지 않은 모든 런이 v13 을 읽는다. 그래서 블록을 그리는 코드와 같은
  변경에 실린다.
