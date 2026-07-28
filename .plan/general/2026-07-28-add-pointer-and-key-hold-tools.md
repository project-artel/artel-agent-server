# 2026-07-28 — QA 에이전트 마우스 이동·드래그·키 홀드 도구 추가

- Date: 2026-07-28
- Jira: ARTEL-168
- Status: Done

## Goal

QA 에이전트가 SDK의 신규 ACTION 5종(`move_mouse`, `mouse_down`, `mouse_up`,
`key_down`, `key_up`)을 도구로 부를 수 있게 한다. 지금 에이전트가 할 수 있는
조작은 버튼 클릭·텍스트 입력·키 한 번 누르기뿐이라, 포인터를 좌표로 옮기거나
버튼·키를 누른 채로 두어야 하는 단계는 시나리오에 적혀 있어도 수행할 방법이 없다.

특히 드래그 앤 드랍은 `mouse_down → move_mouse → mouse_up`을 **한 배치**로
보내야 성립한다. SDK의 ACTION 큐가 직렬이라 배치 안의 순서와 프레임 간격이
보장되는 것이 유일한 근거이고, 세 번의 도구 호출로 나누면 그 사이에 다른 액션이
끼어들 수 있다.

## Non-goals

- 오케스트레이션 서버 변경. `ActionItemDto.method`는 non-blank만 검사하는 String
  이라 신규 메서드도 그대로 중계된다(ARTEL-169가 회귀 테스트로 고정).
- SDK 변경. ARTEL-154가 담당한다.
- 씬 렌더에 화면 좌표를 싣는 일. 오케스트레이션의 `GameStateTransformer`가
  아직 블록의 `transform.rect`/`screen`을 에이전트로 넘기지 않는다(아래 Risks).
- `button_click`을 포인터 경로로 대체하는 일. 두 경로는 별개로 공존한다.
- 웨이포인트를 여러 개 받는 곡선 드래그. `move_mouse` 자체가 커서를 여러 프레임에
  걸쳐 보간하므로 시작·끝 두 점이면 uGUI 드래그 인터페이스가 모두 호출된다.

## Context / Constraints

- 프로토콜(ARTEL-154 확정):
  - `move_mouse`, params `[x, y]` — 화면 픽셀 좌표. **정정(ARTEL-171): 원점은
    좌상단이다. SDK가 씬 rect와 같은 좌상단 픽셀을 받아 내부에서 Unity의 좌하단
    화면 좌표로 뒤집으므로, 호출하는 쪽은 어떤 변환도 하지 않는다.**
  - `mouse_down` / `mouse_up`, params `[]` 또는 `[button]` — 0=좌, 1=우, 2=휠, 기본 0.
  - `key_down` / `key_up`, params `[keyCode]` — Unity KeyCode 이름 또는 숫자값.
- `mouse_down`은 좌표를 받지 않는다. 누르는 지점은 **그때의 커서 위치**다. 따라서
  드래그 배치는 `move_mouse(시작) → mouse_down → move_mouse(끝) → mouse_up`이어야
  하고, 시작점으로 옮기는 첫 이동을 빠뜨리면 엉뚱한 곳에서 눌린다.
- `app/agents/qa/tools.py`의 `_run` 헬퍼는 액션 하나만 받는다. 결과 렌더링도
  `item.id > 1`로 후행 `scan_scene`을 걸러내므로 배치를 그대로 태울 수 없다.
- 실제로 에이전트에게 가는 시스템 프롬프트는 `app/agents/qa/runner.py`의
  `SYSTEM_PROMPT`다. `app/agents/qa/prompt.py`의 `ACT_SYSTEM`은 구(舊) 단계별
  act/evaluate 경로(`QaExecutionAgent`)의 것으로 지금 라우트에 연결되어 있지
  않지만, 사용 가능한 SDK 메서드 목록을 열거하고 있어 방치하면 목록이 어긋난다.
- 홀드는 새는 상태다. SDK는 연결 종료 시 전부 해제하지만, 연결이 살아 있는 동안
  에이전트가 잊으면 눌린 채 남아 이후 모든 단계의 판정을 오염시킨다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료. `tools.py`/`channel.py`/`envelope.py`/`runner.py`,
      ARTEL-154 SDK 플랜, 오케스트레이션 `GameStateTransformer` 확인.

- [x] **Step 1: `_run`을 배치 헬퍼로** — `app/agents/qa/tools.py`
  - 시그니처를 `actions: list[JsonRpcAction]`으로 바꾸고 기존 세 호출부를
    한 원소 리스트로 감싼다. 형제 헬퍼를 새로 만들지 않는다 — 로그·결과 렌더·
    씬 첨부가 전부 같고, 복제하면 두 벌이 갈라진다.
  - 결과 필터를 `item.id > 1`에서 "배치에 있는 id인가"로 바꾼다. 배치가 N개면
    후행 `scan_scene`은 N+1번이다.
  - 각 줄에 메서드 이름을 붙인다. 드래그는 결과가 네 줄이라 라벨이 없으면
    어느 것이 실패했는지 읽을 수 없다.

- [x] **Step 2: 신규 도구 5종** — 기존 스타일(async, `step`/`thought` 인자,
      모델이 읽을 docstring) 그대로.
  - `move_pointer(step, x, y, thought)` → `move_mouse [x, y]`
  - `hold_mouse_button(step, thought, button=0)` → `mouse_down [button]`
  - `release_mouse_button(step, thought, button=0)` → `mouse_up [button]`
  - `hold_key(step, key_code, thought)` → `key_down [key_code]`
  - `release_key(step, key_code, thought)` → `key_up [key_code]`
  - `drag_pointer(step, from_x, from_y, to_x, to_y, thought, button=0)` →
    `[move_mouse, mouse_down, move_mouse, mouse_up]` 한 배치.
  - 이름은 모델이 읽을 것이므로 `click_button`/`enter_text`/`press_key`와 같은
    어법(동사_명사)으로 둔다.

- [x] **Step 3: 프롬프트** — `runner.py`의 `SYSTEM_PROMPT`에 언제 쓰는지와
      "누른 것은 반드시 푼다"를 넣고, `prompt.py`의 `ACT_SYSTEM` 메서드 목록에
      신규 5종을 더해 두 목록이 어긋나지 않게 한다.

- [x] **Step 4: Tests** — `tests/test_qa_tools.py`에 기존 스타일로 추가.
      메서드 이름, params, 그리고 드래그 배치의 **순서**를 고정한다.

- [x] **Step 5: Rollout / Rollback** — 순수 추가. 기존 도구의 와이어 출력은
      바뀌지 않는다(결과 텍스트에 메서드 라벨이 붙는 것만 달라진다).

## Validation

- **Commands to run:** `python -m pytest` (project.md / Dockerfile test 스테이지)
- **Expected output:** 기존 99건 + 신규 테스트 전부 통과
- **Result:** `104 passed` (변경 전 기준선 99건 + 신규 5건), 실패 0.

## Risks & Rollback

- **Risks:**
  - **좌표가 에이전트에게 도달하지 않는다.** SDK는 블록마다
    `transform.rect`(픽셀, 원점 **좌상단**)와 씬 단위 `screen`(w/h)을 싣지만,
    오케스트레이션의 `GameStateTransformer`가 `Interactable`을
    `id/name/type/label/placeholder/actions`로만 줄여 넘긴다. 좌표 릴레이가
    붙기 전까지 `move_pointer`/`drag_pointer`는 에이전트가 좌표를 알아낼 방법이
    없다. 별건으로 올려야 한다.
  - ~~**원점이 서로 다르다.**~~ **정정(ARTEL-171): 이 위험은 존재하지 않았다.**
    `move_mouse`는 씬 rect와 같은 좌상단 원점 픽셀을 받고 Unity 좌표계로의
    변환은 SDK 내부에서 일어난다. `y_mouse = screen.h - y_rect`를 시키면 오히려
    화면 반대편을 찍는다. 이 서술을 따라 들어갔던 프롬프트·docstring의 변환
    지시는 ARTEL-171에서 전부 제거했다.
  - **홀드 누수.** `hold_*` 후 `release_*`가 없으면 이후 단계가 오염된다.
    프롬프트로만 막으므로 모델이 어기면 남는다. 런 종료 시 서버가 강제 해제하는
    것은 이번 범위 밖.
  - SDK가 아직 배포되지 않은 상태에서 이 도구를 부르면 SDK가
    `Unsupported method`로 실패를 돌려준다. 실패가 결과 문자열로 그대로 오므로
    에이전트가 판단할 수 있고, 런이 죽지는 않는다.
- **Rollback steps:** 단일 feature commit `git revert`.

## Open Questions

- 좌표 릴레이(오케스트레이션 `GameStateTransformer` + 에이전트 `SceneMemory`
  렌더)를 어느 이슈로 뺄지. 그것 없이는 이번 도구 중 포인터 계열이 실전에서
  동작하지 않는다.
