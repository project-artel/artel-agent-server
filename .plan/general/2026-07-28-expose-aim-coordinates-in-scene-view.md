# 2026-07-28 — Agent 씬 뷰에 조준 좌표 노출

- Date: 2026-07-28
- Jira: ARTEL-171
- Status: Done

## Goal

에이전트가 읽는 씬 뷰(`SceneMemory.render`)에 각 인터랙터블의 **화면 좌표**를
싣는다. ARTEL-168이 붙인 `move_pointer`/`drag_pointer`는 픽셀을 받는데, 지금
씬 뷰는 id·name·type·label만 출력한다. 좌표를 알 방법이 없으니 포인터 계열
도구는 실전에서 쓸 수 없는 상태다(ARTEL-168 플랜의 Open Question).

찍어야 할 점은 요소의 **중심**이므로 중심을 그대로 출력한다. 모델에게
`x + w/2` 산수를 시키면 틀릴 여지만 생긴다.

## Non-goals

- 오케스트레이션 변경. `GameStateTransformer`가 `rect`/`onScreen`/`screen`을
  중계하는 일은 ARTEL-170이 병행 진행한다.
- SDK 변경. 좌표 원점 처리는 SDK가 이미 담당한다(아래 Context).
- 좌표로 클릭하는 새 도구. `button_click`(id)과 포인터(픽셀)는 그대로 공존한다.
- 오프스크린 요소를 화면 안으로 스크롤해 주는 자동화. 에이전트가 알아서 한다.

## Context / Constraints

- 들어오는 계약(ARTEL-170과 동일해야 한다):
  - 인터랙터블마다 `rect`: `{x, y, w, h}` (픽셀, 원점 **좌상단**, `x`/`y`는
    요소의 좌상단 모서리) 또는 없음/null.
  - 인터랙터블마다 `onScreen`: bool, 기본 true.
  - 게임 상태에 `screen`: `{w, h}` 또는 없음/null.
- **좌표 변환은 필요 없다.** SDK의 `move_mouse`는 씬이 보고하는 것과 같은
  좌상단 원점 픽셀을 받고, 내부에서 Unity의 좌하단 화면 좌표로 뒤집는다.
  ARTEL-168은 반대로 알고 프롬프트·docstring에 "원점 좌하단, 위에서 재면
  변환하라"고 적어 두었다. 그 지시가 남아 있으면 에이전트는 y를 한 번 더
  뒤집어 정확히 화면 반대편을 찍는다. 이번 작업에서 전부 걷어낸다.
- 구(舊) 오케스트레이션 서버는 세 필드를 하나도 보내지 않는다. 전부 선택
  필드여야 하고, 없으면 지금과 똑같이 렌더되어야 한다.
- `Interactable`/`GameState`는 `extra="allow"`라 지금도 값은 실려 오지만,
  이름 붙은 필드가 아니면 렌더가 읽을 수 없다.
- 오프스크린 요소도 `rect`는 값이 있다(화면 밖 좌표). 그 좌표를 찍으면 아무
  것도 안 눌리므로, 좌표 대신 "화면 밖"이라고 말해야 한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/qa/scene.py`(`SceneMemory.render`),
      `app/qa/envelope.py`(`Interactable`/`GameState`), `app/agents/qa/tools.py`,
      `app/agents/qa/runner.py`(`SYSTEM_PROMPT`), `app/agents/qa/prompt.py`
      (`ACT_SYSTEM`), `tests/test_qa_scene.py`.

- [x] **Step 1: 와이어 모델** — `app/qa/envelope.py`
  - `Rect(x, y, w, h)`와 `Screen(w, h)`를 추가하고, 중심 계산은 `Rect.center`
    프로퍼티 한 곳에 둔다. 렌더와 테스트가 각자 계산하면 갈라진다.
  - `Interactable`에 `rect: Rect | None = None`, `onScreen: bool = True`.
  - `GameState`에 `screen: Screen | None = None`.

- [x] **Step 2: 씬 메모리** — `app/qa/scene.py`
  - `SceneMemory.screen`은 보고된 값이 오면 덮어쓰고, 프레임이 생략하면
    직전 값을 유지한다. 화면 크기는 프레임이 아니라 창의 속성이라, 생략은
    "안 바뀜"이지 "없어짐"이 아니다(인터랙터블의 교체 규칙과 다른 이유).
  - `render`의 "you can act on:" 줄에 `@ 중심x,중심y wxh`를 붙인다.
    `[12] Start (button) @ 520,330 200x60 — 시작`.
  - `onScreen=false`면 좌표 대신 `(off screen)`.
  - `screen`이 오면 씬 머리줄 아래에 `screen: 1920x1080` 한 줄.
  - 셋 다 없으면 출력은 지금과 글자 하나까지 같다.

- [x] **Step 3: 프롬프트 정정** — 좌하단/변환 지시 제거
  - `runner.py` `SYSTEM_PROMPT`: 씬이 찍어 준 숫자를 **그대로** 포인터 도구에
    넣는다. 변환 금지. 오프스크린은 조준 불가.
  - `tools.py` `move_pointer`/`drag_pointer` docstring 동일 취지로 교체.
  - `prompt.py` `ACT_SYSTEM`의 `move_mouse` 항목(구 act/evaluate 경로. 지금
    라우트에 물려 있지 않지만 방치하면 목록이 어긋난다). 이쪽은 씬 JSON을
    그대로 받으므로 `rect`가 좌상단 모서리라는 것과 중심을 조준한다는 것을
    같이 적는다.
  - ARTEL-168 플랜의 잘못된 서술에 정정 표기.

- [x] **Step 4: Tests** — `tests/test_qa_scene.py`에 기존 스타일로 추가.
      중심·크기 출력, 오프스크린, 화면 크기, 그리고 **필드가 없는 구 페이로드**의
      렌더가 그대로인지.

- [x] **Step 5: Rollout / Rollback** — 순수 추가. ARTEL-170보다 먼저 배포되면
      필드가 안 와서 지금과 동일하게 동작한다.

## Validation

- **Commands to run:** `python -m pytest` (project.md / Dockerfile test 스테이지)
- **Expected output:** 기준선 104건 + 신규 테스트 전부 통과
- **Result:** `108 passed` (변경 전 기준선 104건 + 신규 4건), 실패 0.

## Risks & Rollback

- **Risks:**
  - **계약 불일치.** 필드 이름(`rect`/`onScreen`/`screen`)과 의미가 ARTEL-170과
    한 글자라도 다르면 좌표가 조용히 사라진다(선택 필드라 검증 오류도 안 난다).
    이름은 SDK가 내보내는 그대로 쓴다.
  - **중심 반올림.** 정수 나눗셈이라 홀수 폭에서 최대 1픽셀 왼쪽/위로 치우친다.
    버튼 크기에 비하면 무의미하고, 소수점을 찍으면 뷰만 지저분해진다.
  - **오래된 y 변환 지시가 남는 것.** 하나만 남아도 에이전트는 화면 반대편을
    찍는다. 프롬프트·docstring·플랜 전부를 grep으로 확인한다.
- **Rollback steps:** 단일 feature commit `git revert`.

## Open Questions

- 없음.
