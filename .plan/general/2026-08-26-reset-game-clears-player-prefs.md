# 2026-08-26 — reset_game 에 저장 데이터 초기화 요청 추가

- Date: 2026-08-26
- Jira: ARTEL-500
- Status: Implemented (pair review folded in), PR draft open

## Goal

`reset_game` 이 씬 리로드만이 아니라 게임의 `PlayerPrefs` 까지 지우도록 요청할 수
있게 한다. SDK 쪽(ARTEL-499)이 새 wire 파라미터를 받도록 배우고 있고, 이 이슈는
서버가 그 파라미터를 보낼 수 있게 하는 절반이다.

Wire 모양은 이미 정해져 있다:

```json
{"id": 1, "jsonrpc": "2.0", "method": "reset_game", "params": [{"clearPlayerPrefs": true}]}
```

wire 는 camelCase(`capture_screen` 의 `maxEdge` / `padding` 과 같은 규칙),
Python tool 인자는 snake_case.

닿는 곳은 둘이다.

1. QA 에이전트가 직접 부르는 `reset_game` tool — 한 스텝이 "처음 실행하는 게임"을
   전제로 할 때 에이전트가 스스로 요청한다.
2. 시나리오 사이 초기화 정책 `FullResetPolicy` — 두 번째 이후 시나리오가 첫 실행
   상태에서 출발하게 하는 옵션. 첫 시나리오는 run 루프가 정책을 부르지 않아 제외다.

## Non-goals

- **`FullResetPolicy(clear_player_prefs=...)` 를 request 필드·설정·환경변수에 연결하지
  않는다.** 아무도 요청하지 않았고, "다음 시나리오의 사전조건이 이전 시나리오의 저장
  데이터인가"는 제품 결정이다. 기본값은 지금과 같은 `False` 로 두고, 주입할 수 있는
  자리만 만든다.
- prompt 버전 bump 없음. `app/prompts/` 어디에도 `reset_game` 이 등장하지 않으므로
  프롬프트 텍스트가 바뀌지 않는다.
- Kotlin orchestration 서버 변경 없음. `params: List<Any>` 를 그대로 중계한다.
- 파일 저장(save file)까지 지우는 기능은 범위 밖이다. `PlayerPrefs` 만 지운다.

## Context / Constraints

- `app/agents/qa/tools.py:1320-1344` — 현재 `reset_game(step, thought)`.
- `app/agents/qa/reset.py:29-48` — `FullResetPolicy`, `DEFAULT_RESET_POLICY`.
- `app/qa/envelope.py:420-432` — `JsonRpcAction.params: list` 가 이미 dict 를 담을 수
  있다. 변경 불필요.
- `app/agents/qa/arch.py:316-346` — `arch_fingerprint` 가 `tool.args` 를 해시한다.
  `clear_player_prefs` 가 추가되면 fingerprint 가 움직인다. 그 함수의 docstring 이
  "signature 가 바뀐 tool 은 모델에게 다른 tool"이라고 의도를 명시하고 있고, 고정된
  golden digest 는 없다(`tests/test_qa_arch.py` 는 관계 속성만,
  `tests/test_qa_run_config_contract.py:50` 은 `len == 12` 만 본다). 결과: 머지 전후
  run 은 fingerprint 로 더 이상 같은 버킷에 묶이지 않는다 — 의도된 것이며 PR 에 적는다.
- **docstring 은 영어로 둔다.** `coding-style.md` 의 한국어 규칙은 comment 에 적용된다.
  tool docstring 은 comment 가 아니라 LangChain 이 `tool.description` 으로 뽑아 모델에게
  그대로 먹이는 프롬프트다(`tests/test_qa_tools.py:905` 가 `tool.description` 을 검사).
  이 파일의 모든 tool docstring 이 영어다(`click_button:1091`, `press_key:1114`,
  `set_input_axis:1206`, `resume_game_time:1309`). 소스 comment 는 한국어로 쓴다.
- 하위 호환: 플래그가 꺼져 있으면 `params` 를 아예 비운다. 그래야 기본 호출의 wire 가
  지금과 byte 단위로 같고, 옛 SDK 는 아무 변화도 보지 못한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 위 Context 의 파일·라인 전부 확인. `app/prompts/` grep 결과
      `reset_game` 히트 0.
- [x] **Step 1: Implementation**
  - `app/agents/qa/tools.py` — `reset_game(step, thought, clear_player_prefs: bool = False)`.
    기본값 있는 인자는 맨 뒤(`hold_mouse_button(step, thought, button: int = 0)`,
    `capture_screen(step, thought, target_id: int | None = None)` 과 같은 배치).
    본문은 `params: list[Any] = [{"clearPlayerPrefs": True}] if clear_player_prefs else []`.
    docstring 재작성: 플래그가 하는 일(게임의 `PlayerPrefs` 삭제, SDK 자신의 `Artel.*`
    항목은 보존), 여전히 닿지 못하는 것(디스크 파일 — 그래서 기존의 "operator 가 필요"
    탈출구가 그 경우엔 그대로 유효), 되돌릴 수 없고 이후 모든 스텝·시나리오가 결과를
    물려받는다는 것, 언제 쓰고 언제 쓰지 않는지, 그리고 이 플래그 이전 SDK 로 만든
    게임은 플래그를 무시하고 씬 상태만 되돌리며 tool 은 그것을 알 수 없다는 한 문장
    (그러니 wipe 에 기댄 스텝은 재시도가 아니라 보고 대상 — `capture_screen` 의 실패
    분기 `:408-411` 이 선례). 과대 약속 금지: store 를 비울 뿐 첫 실행 상태를 보장하지
    않는다(리로드로 파괴되는 매니저가 `OnDestroy` 에서 키를 다시 쓸 수 있다).
  - `app/agents/qa/reset.py` — `FullResetPolicy.__init__(clear_player_prefs: bool = False)`,
    `between_scenarios` 가 켜졌을 때만 `params=[{"clearPlayerPrefs": True}]` 를 보낸다.
    `DEFAULT_RESET_POLICY = FullResetPolicy()` 그대로. 모듈 docstring 에 저장 데이터가
    이제 정책이 정의하는 시작 상태 안에 들어왔고 기본값은 그대로라는 한국어 문장 추가,
    클래스 docstring 에 이 옵션을 켜면 (첫 시나리오를 뺀) 시나리오가 첫 실행 게임에서
    출발하며 그것은 다른 QA 계약이라는 한국어 문장 추가.
  - tool 목록 등록(`tools.py:1600`)은 그대로. 이름이 바뀌지 않는다.
- [x] **Step 2: Tests**
  - `tests/test_qa_tools.py`
    - `test_resetting_goes_out_as_the_reset_action` — 유지하되 `params == []` 단언 추가.
      기본 호출의 하위 호환을 이 단언이 고정한다.
    - 신규 `test_a_reset_can_ask_for_the_player_prefs_to_go` — `clear_player_prefs=True`
      로 호출, 나간 action 이 정확히
      `{"id": 1, "jsonrpc": "2.0", "method": "reset_game", "params": [{"clearPlayerPrefs": True}]}`.
    - 신규 `test_the_reset_tool_says_what_the_wipe_does_not_reach` —
      `tools["reset_game"].description` 이 `PlayerPrefs` 와 save file 한계를 언급하는지.
      한계 문장은 통째로 고정한다 — 조각 단언은 변경 이전 docstring 에도 통과하므로
      아무것도 구별하지 못한다(pair review 지적).
    - `test_the_agent_is_offered_exactly_these_tools:864`,
      `test_every_tool_takes_a_thought:925` — 변경 없이 통과 확인.
  - `tests/test_qa_reset.py`
    - `test_full_reset_dispatches_reset_game:17-36` — 변경 없음. 그 `params: []` 단언이
      이제 새 생성자 기본값까지 고정한다.
    - 신규 `test_a_full_reset_can_be_asked_to_clear_player_prefs` —
      `FullResetPolicy(clear_player_prefs=True)`, `params == [{"clearPlayerPrefs": True}]`.
      즉시 응답은 `answer_first_action` 헬퍼로 공유해 30초 dispatch 타임아웃을 피한다.
- [x] **Step 3: Rollout / Rollback**
  - **머지 순서: SDK PR(ARTEL-499)이 먼저 머지된다.** 새 서버가 옛 SDK 를 만나면
    플래그가 조용히 버려진다 — 리셋은 성공하고 저장 데이터는 살아남고 아무도 보고하지
    않는다. ACTION 프로토콜에 이를 감지할 버전 필드가 없다.
  - 롤백은 revert 한 번. 기본 경로의 wire 가 바뀌지 않으므로 되돌려도 옛 SDK·새 SDK
    양쪽에서 지금과 같이 동작한다.

## Validation

- **Commands to run:** 워크트리 안에서
  `/home/yunseong/dev/artel/artel-agent-server/.venv/bin/python -m pytest`
  (`project.md` 가 `python -m pytest` 로 문서화; 워크트리에 `.venv` 가 없어 메인
  체크아웃의 인터프리터를 쓴다)
- **Expected output:** 신규 3건 포함 전부 통과. 사전 존재 실패
  `tests/test_config.py::test_settings_can_load_from_env_file` 1건은 이 변경과 무관
  (환경의 `OPENROUTER_API_KEY` 가 `.env` 픽스처 값을 덮어쓴다). baseline 은
  `1 failed, 644 passed`.

## Risks & Rollback

- **Risks:**
  - 옛 SDK 에서 플래그가 조용히 무시된다. 프로토콜에 감지 수단이 없으므로 완화책은
    머지 순서와 docstring 이 전부다. docstring 이 에이전트에게 "재시도하지 말고
    보고하라"고 말한다.
  - `arch_fingerprint` 가 움직여 머지 전후 run 이 같은 버킷으로 묶이지 않는다. 의도된
    것이지만 비교 리포트를 보는 사람에게는 놀랄 일이라 PR 에 명시한다.
  - `FullResetPolicy` 옵션은 지금 아무도 주입하지 않는다. 리뷰어가 YAGNI 를 부르면
    떼어낼 수 있는 조각이며, tool 변경과 커밋을 분리해 그렇게 만든다.
- **Rollback steps:** `git revert`. 기본 wire 가 불변이라 부분 롤백도 안전하다.

## Pair Review 반영

`pair-review` critic 이 VERDICT: NONPASS 를 냈고, 아래를 접었다.

- **must-fix — `FullResetPolicy` 가 광고한 계약을 지키지 못했다.** 클래스 docstring 이
  "모든 시나리오가 처음 실행하는 게임에서 출발한다"고 썼지만, `QaExecutionService.run`
  (`app/qa/service.py:124-129`)이 `if index > 0` 로 첫 시나리오 앞에서는 정책을 부르지
  않는다. 첫 시나리오는 기기에 남아 있던 PlayerPrefs 를 그대로 만난다. docstring 에
  이 구멍을 명시했다.
- **must-fix — tool docstring 이 스스로 모순됐다.** 플래그 없는 문단이 "a tutorial that
  plays once a session"을 이유로 들고, 플래그 문단이 같은 예("a tutorial gate that plays
  once a session")를 들었다. 세션 단위 게이트는 씬 리로드로 이미 사라지므로 플래그가
  필요 없다. 플래그 문단을 "once per install rather than once per session"으로 바꾸고,
  세션 단위 게이트에는 플래그가 아무것도 사주지 않는다는 문장을 넣었다.
- **should-fix — description 테스트가 아무것도 구별하지 못했다.** `"disk"`,
  `"save file"`, `"operator"` 는 변경 이전 docstring 에도 있었다. 한계 문장을 통째로
  고정하도록 바꾸고, 왜 취약함을 감수하는지 테스트 docstring 에 적었다.
- **should-fix — `answer()` 클로저 복붙.** `answer_first_action` 헬퍼로 올렸다.
- **nit — "files on disk"** 는 PlayerPrefs 자체가 파일인 플랫폼(macOS plist, Linux
  `~/.config`, Android XML)에서 문자 그대로 거짓이다. "the game's own save files"로.
- **nit — 주석 타입 표기 불일치.** `reset.py` 도 `list[Any]`.
- **거절 — "`FullResetPolicy` 옵션은 호출자가 없으니 빼라"(YAGNI).** 이슈가 명시적으로
  요구한 범위이고, 커밋을 분리해 독립적으로 떼어낼 수 있게 뒀다. PR 에 리뷰어가 YAGNI 를
  부르면 떼어낼 조각이라고 적는다. 91자 라인은 이 저장소에 린터 설정이 없고 `app/` 에
  더 긴 줄이 많아 그대로 둔다.

## Open Questions

- 없음. wire 모양과 non-goal 경계 모두 이슈에서 확정되었다.
