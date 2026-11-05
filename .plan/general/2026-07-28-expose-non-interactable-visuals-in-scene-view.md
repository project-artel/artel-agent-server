# 2026-07-28 — Agent 씬 뷰에 비-인터랙터블 시각 요소 노출

- Date: 2026-07-28
- Jira: ARTEL-175
- Status: Done

## Goal

씬 뷰(`SceneMemory.render`)에 인터랙터블이 아닌 **화면 위 시각 요소**(`visuals`)를
자기 섹션으로 싣고, 거기에 붙은 좌표도 포인터 도구로 누르거나 끌 수 있다는 것을
프롬프트에 명시한다.

ARTEL-171이 인터랙터블에 조준 좌표를 붙였지만, 에이전트가 볼 수 있는 것은 여전히
"누를 수 있는 것" 목록뿐이다. 버튼이 아닌 스프라이트를 드래그하는 시나리오는
좌표를 얻을 경로가 없어 실행이 불가능하다. ARTEL-174가 중계하는 `visuals`가 그
경로이고, 이번 작업은 그것을 뷰와 프롬프트에 연결한다.

## Non-goals

- 오케스트레이션 변경. `visuals` 수집·중계는 ARTEL-174가 병행한다.
- SDK 변경. 좌표 원점 처리는 SDK가 이미 담당한다.
- 새 도구. `move_pointer`/`drag_pointer`가 이미 픽셀을 받으므로 그대로 쓴다.
- 렌더러·씬 모델 구조 개편. 섹션 하나와 필드 하나만 늘린다.
- `visuals`를 인터랙터블처럼 id로 조작하는 경로. id는 표시용이며, SDK의
  `button_click`이 받는 대상이 아니다.

## Context / Constraints

- 들어오는 계약(ARTEL-174와 동일해야 한다):

  ```json
  "visuals": [
    {"id": 44, "name": "enemy_goblin", "type": "sprite", "sprite": "goblin_idle",
     "rect": {"x": 252, "y": 372, "w": 96, "h": 96}, "onScreen": true}
  ]
  ```

  - `type`은 `image`(uGUI Image) 또는 `sprite`(SpriteRenderer).
  - `sprite`는 스프라이트 에셋 이름. 단색 Image처럼 없을 수 있다.
  - `rect`는 모를 때 없다. `onScreen` 기본값 true.
  - `rect`는 픽셀, 원점 **좌상단** — 포인터 도구가 받는 것과 같은 공간이다.
    **변환은 절대 없다.**
  - `interactables`에 이미 실린 요소는 `visuals`에 다시 오지 않는다.
- 구(舊) 오케스트레이션 서버는 `visuals`를 아예 보내지 않는다. 선택 필드여야
  하고, 없으면 렌더는 글자 하나까지 지금과 같아야 한다.
- 좌표 표기는 `_where()` 하나를 재사용한다. 시각 요소가 인터랙터블과 다른
  문법으로 찍히면 에이전트는 둘을 다른 종류의 좌표로 읽는다.
- 오프스크린 시각 요소도 `rect`는 값이 있다. 그 좌표를 찍으면 아무것도 안
  눌리므로 좌표 대신 "화면 밖"이라고 말한다 — `_where()`가 이미 그렇게 한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/qa/envelope.py`(`Interactable`/`GameState`),
      `app/qa/scene.py`(`_where`, `SceneMemory.render`), `app/agents/qa/`
      (`tools.py`/`runner.py`/`prompt.py`), `tests/test_qa_scene.py`.

- [x] **Step 1: 와이어 모델** — `app/qa/envelope.py`
  - `Visual(id, name, type, sprite=None, rect=None, onScreen=True)`.
    `rect`/`onScreen`은 `Interactable`과 같은 의미·같은 기본값이어야 `_where()`가
    양쪽에 그대로 통한다.
  - `GameState.visuals: list[Visual] = []`.

- [x] **Step 2: 씬 메모리** — `app/qa/scene.py`
  - `_where()`가 `Interactable | Visual`을 받도록 타입만 넓힌다. 본문은 그대로.
  - `SceneMemory.visuals`. 인터랙터블과 같은 이유로 병합이 아니라 **교체**.
  - `render`에 `on screen:` 섹션을 actionable 목록 **뒤에** 붙인다. 비어 있으면
    섹션 자체를 내지 않는다.
    `  [44] enemy_goblin (sprite) @ 300,420 96x96`

- [x] **Step 3: 프롬프트** — 이번 이슈의 핵심
  - `runner.py` `SYSTEM_PROMPT`: `on screen:` 아래 좌표가 붙은 것은 액션 목록에
    없어도 포인터 도구로 누르거나 끌 수 있다고 적는다. 버튼이 아닌 스프라이트를
    끄는 길은 그것뿐이라는 것도. 주변 문체 유지, 분량은 몇 문장.
  - `prompt.py` `ACT_SYSTEM`: 씬 JSON을 그대로 받는 경로라 `visuals`가 무엇인지
    한 줄. 좌표 규칙은 `move_mouse` 항목에 이미 있으므로 반복하지 않는다.

- [x] **Step 4: Tests** — `tests/test_qa_scene.py`에 기존 스타일로 추가.
      중심·크기 출력, 오프스크린 표기, 그리고 `visuals` 없는 페이로드의 렌더가
      그대로인지.

- [x] **Step 5: Rollout / Rollback** — 순수 추가. ARTEL-174보다 먼저 배포되면
      필드가 안 와서 지금과 동일하게 동작한다.

## Validation

- **Commands to run:** `uv run --extra dev python -m pytest -q`
- **Expected output:** 기준선 108건 + 신규 3건 통과, 실패 0.
- **Result:** `111 passed` (변경 전 기준선 108건 + 신규 3건), 실패 0.

## Risks & Rollback

- **Risks:**
  - **계약 불일치.** 필드 이름(`visuals`/`sprite`/`rect`/`onScreen`)이 ARTEL-174와
    한 글자라도 다르면 요소가 조용히 사라진다(선택 필드라 검증 오류도 안 난다).
  - **뷰 비대화.** 씬 하나에 시각 요소가 수백 개인 게임이면 `on screen:` 섹션이
    프롬프트를 삼킨다. 상한은 보내는 쪽(ARTEL-174)이 쥐고 있고, 실제 프레임을
    보기 전에 여기서 숫자를 정하면 근거 없는 상한이 된다. 나오면 그때 자른다.
  - **id 혼동.** `visuals`의 id는 표시용인데 에이전트가 `click_button`에 넣을 수
    있다. 프롬프트에서 이쪽 접근 경로가 포인터라는 것을 분명히 해서 줄인다.
- **Rollback steps:** 단일 feature commit `git revert`.

## Open Questions

- 없음.
