# 2026-09-01 — QA tool 정의를 주제별 모듈로 나눈다

- Date: 2026-09-01
- Jira: ARTEL-688
- Status: Reviewed (fast·medium 1차, heavy 2차, pair review 반영). `f82c597` 로 rebase 완료

## Goal

`app/agents/qa/tools.py` 2347 줄을 `app/agents/qa/tools/` package 로 바꾸고, `build_tools`
한 함수(421-2347 줄, 1927 줄) 안에 중첩돼 있는 tool 36 개를 주제별 모듈로 옮긴다. 옮기기만
한다. tool 이름, 인자 schema, description, 본문, 조립 순서를 하나도 바꾸지 않는다.

줄 번호와 개수는 base `f82c597` 기준이다. 작업 도중 develop 이 움직였고(#151 이 capability
tool 셋과 `app/agents/qa/capability.py` 를 넣었다), 거기로 rebase 하면서 주제가 다섯에서
여섯으로 늘었다. 아래 `## Rebase` 참고.

## Non-goals

- tool 동작 변경. 이번 diff 는 순수 이동이다.
- tool 추가나 삭제, description 문구 수정.
- `tests/test_qa_tools.py` 1656 줄을 쪼개는 일.
- `app/specs_v2/discovery.py` 1601 줄.
- `app/agents/scenario/tools.py` 198 줄.

## Context / Constraints

### 지금 모양

`build_tools(channel, state, arch)` 안에 전부 들어 있다. 파일 안의 순서가 이미 주제별이다.

| 덩어리 | 줄 | tool |
| --- | --- | --- |
| 공통 헬퍼 `_answer` `_run`, 화면 관찰 `observe_scene` `inspect_object` `capture_screen` | 426-651 | 3 |
| 지식 `search_knowledge`~`expand_knowledge` | 653-1315 | 7 |
| screen selector `_write_screen_selector_rule`, `include_screen_selector`, `exclude_screen_selector` | 1317-1419 | 2 |
| capability `_standing_scene`~`list_scene_capabilities` | 1421-1736 | 3 |
| 게임 입력 `click_button`~`reset_game` | 1738-2062 | 16 |
| 보고와 운영자 `wait_for_operator`~`reply_to_operator` | 2064-2297 | 5 |
| 조립과 `arch.vision` 분기 | 2299-2347 | — |

모듈 수준에는 `PendingCapture` 86-92, `QaRunState` 95-306, `CAPTURE_SCREEN_DESCRIPTION`
313-325, `REPORT_ISSUE_DESCRIPTION` 330-350, `render_closing_asks` 353-418 이 있다.

tool 안에 또 중첩된 헬퍼도 함께 간다. `update_knowledge` 안의 `refused` 가 그것이다.

### 나누지 못하게 잡고 있는 것

1. tool 36 개가 전부 `channel`, `state`, `arch` 를 closure 로 잡는다.
2. `_answer` 는 화면과 운영자의 말을 붙이는 유일한 자리다. `state.watermark` 를 옮긴다.
   여섯 덩어리 중 네 덩어리가 부른다.
3. `_run` 은 acting tool 전부가 지나간다. `state.last_action_frame` 을 옮기고 `_answer` 를
   부른다.
4. `app/agents/qa/arch.py` 의 `structure_of` 가 `_ThrowawayChannel` 로 `build_tools` 를 불러
   tool 이름과 `tool.args` 를 읽고 fingerprint 를 낸다. tool 하나라도 조립에서 빠지면
   digest 가 움직인다.
5. tool 목록의 **순서**도 지켜야 한다. `app/qa/run_config.py:131` 이 `structure_of` 의 이름
   목록을 받아 `tools: list[str]` 로 run config 에 저장한다(`run_config.py:83`, `:166`).
   순서가 바뀌면 저장되는 값이 바뀌고, 모델이 받는 tool 목록의 순서도 바뀐다.
6. `QaRunState`, `PendingCapture`, `build_tools` 를 `app.agents.qa.tools` 에서 import 하는
   파일이 14 개다. app 3 개, tests 11 개.

`app/agents/qa/arch.py` 의 import cycle 은 이 작업의 제약이 아니다. `tools.py` 는 지금도
`arch.py` 를 모듈 수준에서 import 하고 있고(`tools.py:13`), cycle 이 닫히지 않는 것은
`arch.py` 쪽 import 가 함수 안에 있기 때문이다(`arch.py:344`). 새 모듈이 `arch.py` 를 모듈
수준에서 import 해도 같다. 1 차 리뷰에서 이 항목의 근거가 틀렸다는 지적을 받아 고쳤다.

### 기준선

refactor 전 `structure_of(default_resolved_arch())` 값. base `f82c597` 를 따로 checkout 해서
찍었다.

- fingerprint `e8e1d4764809`
- middleware `compaction`, `fold_scene_views`, `fold_knowledge_neighbours`, `capture_vision`,
  `log_token_usage`
- 이름 37 개, 이 순서 그대로 (tool 36 개와 compaction middleware 의 `compact_context`):

```
observe_scene, inspect_object, search_knowledge, record_knowledge, update_knowledge,
forget_knowledge, link_knowledge, unlink_knowledge, expand_knowledge,
include_screen_selector, exclude_screen_selector, list_scene_capabilities,
record_capability_verdict, record_new_capability, click_button, enter_text, press_key,
move_pointer, click_at, double_click_at, hold_mouse_button, release_mouse_button,
hold_key, release_key, set_input_axis, set_input_button, drag_pointer, pause_game_time,
resume_game_time, reset_game, wait_for_operator, report_step, report_issue, finish_run,
reply_to_operator, capture_screen, compact_context
```

**fingerprint 은 순서를 보지 않는다.** `app/agents/qa/arch.py:314` 이 `"tools": sorted(...)`
라서, 목록을 통째로 뒤집어도 digest 는 같다. 테스트에도 순서를 고정하는 곳이 없다. 그래서
순서는 위 34 개를 문자열로 대조해서만 지킬 수 있다. 2 차 리뷰에서 나온 지적이다.

여기에 함정이 하나 더 있다. **정의 순서와 조립 순서가 원래부터 다르다.** 두 군데다.
입력 tool 은 정의가 `hold_key` → `set_input_axis` → `set_input_button` → `release_key` 인데
조립은 `hold_key`, `release_key`, `set_input_axis`, `set_input_button` 이고, capability tool
은 정의가 `record_capability_verdict` → `record_new_capability` → `list_scene_capabilities`
인데 조립은 `list_scene_capabilities` 가 먼저다. 새 모듈에서 "파일에 보이는 순서대로" 돌려주면 `release_key` 가 세
칸 밀리고, fingerprint 도 테스트도 그것을 잡지 못한다. 각 builder 의 `return` 목록은 정의
순서가 아니라 **원래 조립 목록** 을 그대로 옮긴다.

테스트 기준선도 같은 commit 에서 찍었다: `1 failed, 860 passed`. 실패는
`tests/test_config.py::test_settings_can_load_from_env_file` 하나이고, 작업 트리의 실제
`.env` 가 `_env_file` 을 이겨서 나는 기존 실패다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 줄 범위, tool 33 개, importer 14 개, 기준선 fingerprint·순서·테스트
  결과 확인 완료.

- [x] **Step 1: package 로 바꾸고 뼈대를 세운다**

  `app/agents/qa/tools.py` 를 지우고 `app/agents/qa/tools/` 를 만들었다. tool 을 만드는 모듈은
  전부 `*_tools.py`, 그렇지 않은 둘은 그냥 이름으로 뒀다. 이 이름 규칙은 1 차 리뷰에서 나온
  것이다 — `app/agents/qa/` 에 이미 `knowledge.py`, `screen.py`, `context.py` 가 있어서 같은
  이름을 쓰면 편집기 탭과 grep 에서 구분이 안 된다.

  - `app/agents/qa/tools/state.py` (206) — `PendingCapture`, `QaRunState`. 68-262 줄 그대로.
  - `app/agents/qa/tools/tool_context.py` (122) — `ToolContext`. frozen dataclass 로 `channel`,
    `state`, `arch` 를 들고, `_answer` 를 `answer` 메서드로, `_run` 을 `run` 메서드로 가진다.
    두 메서드도 첫 줄에서 `channel, state = self.channel, self.state` 로 되묶고, `run` 은
    `_answer = self.answer` 도 되묶는다. 그래야 본문이 글자 하나 안 바뀐다.
  - `app/agents/qa/tools/__init__.py` (50) — `build_tools` 와 공개 이름 `QaRunState`,
    `PendingCapture` 재수출.

- [x] **Step 2: 주제별 모듈로 tool 을 옮긴다**

  각 모듈은 `def build_<주제>_tools(ctx: ToolContext) -> list[BaseTool]` 하나를 낸다.
  builder 첫 줄에서 `ctx` 가 든 것을 되묶는다. 그래야 옮기는 tool 본체가 글자 하나 바뀌지
  않고, diff 가 순수 이동으로 읽힌다. **되묶는 이름은 모듈마다 다르다** — 2 차 리뷰가
  고정 2 줄을 쓰면 죽은 이름이 생긴다고 지적했다. 실제 사용을 세어서 필요한 것만 적었다.

  | 모듈 | 줄 | tool | 되묶는 이름 |
  | --- | --- | --- | --- |
  | `observation_tools.py` | 185 | 3 | `channel`, `state`, `_answer` (`build_capture_tool` 은 `arch` 도) |
  | `knowledge_read_tools.py` | 160 | 2 | `channel`, `state`, `arch` |
  | `knowledge_write_tools.py` | 585 | 5 | `channel`, `state`, `arch` |
  | `knowledge_tools.py` | 17 | — | 없음. 읽기와 쓰기를 순서대로 합치기만 한다 |
  | `screen_tools.py` | 129 | 2 | `channel` |
  | `capability_tools.py` | 361 | 3 | `channel`, `state` |
  | `action_tools.py` | 362 | 16 | `_run` |
  | `reporting_tools.py` | 363 | 5 | `channel`, `state`, `arch`, `_answer` |

  `observation_tools.py` 는 `build_capture_tool` 을 따로 낸다. `capture_screen` 은
  `arch.vision` 이 켜졌을 때만, 그것도 목록 맨 뒤에 붙기 때문이다.
  `reporting_tools.py` 는 `REPORT_ISSUE_DESCRIPTION` 과 `render_closing_asks` 도 가져가되
  재수출하지 않는다 — 부르는 데가 `report_step` 하나뿐이고 밖에서 import 하는 파일이 없다.

- [x] **Step 3: `build_tools` 를 조립 함수로 줄인다**

  `__init__.py` 의 `build_tools` 가 `ctx` 를 만들고 builder 다섯을 순서대로 펼친 뒤,
  `ctx.arch.vision` 이면 `build_capture_tool(ctx)` 을 맨 뒤에 붙인다. `arch.vision` 주석은
  그대로 옮겼고, 목록 순서가 계약이라는 것을 그 자리에 주석으로 적었다.

- [x] **Step 4: 검증** — 아래 Validation 참고. 전부 기준선과 일치한다.

- [ ] **Step 5: Rollout / Rollback** — feature flag 도 migration 도 없다. `git revert` 한 번이면
  되돌아간다.

## Validation

worktree 에는 `.venv` 가 없다. 본 체크아웃의 인터프리터를 쓰고 `PYTHONPATH=.` 로 worktree
쪽 `app` 을 먼저 잡는다. 실제로 그렇게 잡히는지 `app.agents.qa.tools.__file__` 로 확인했다.

| 확인 | 결과 |
| --- | --- |
| `LANGSMITH_TRACING=false PYTHONPATH=. .venv/bin/python -m pytest -q` | `1 failed, 860 passed` — 기준선과 같음. 실패는 기존 `test_settings_can_load_from_env_file` 하나 |
| fingerprint | `e8e1d4764809` — 기준선과 같음 |
| 이름 37 개를 순서까지 문자열 대조 | 일치 |
| middleware 다섯 | 일치 |
| `git diff --stat origin/develop -- tests` | 비어 있음. 테스트 파일은 한 줄도 안 건드렸다 |
| `wc -l app/agents/qa/tools/*.py` | 가장 큰 파일이 `knowledge_write_tools.py` 585 줄 |
| 모듈별 자유 이름 해결 여부 | 전부 해결됨 |
| 원본 블록이 새 모듈에 바이트 단위로 있는지 | 2347 줄 중 2184 줄 일치 |

마지막 항목은 2 차 리뷰가 요구한 기계적 점검이고, 실제로 버그를 하나 잡았다. `ast` 로 각
모듈의 Load 이름에서 그 모듈 안에서 묶이는 이름을 빼면 밖에서 와야 하는 것만 남는다.
`observation_tools.py` 가 `PendingCapture` 를 쓰는데 import 가 빠져 있었다.

## Risks & Rollback

- **Risks:**
  - **fingerprint 은 순서를 증명하지 않는다.** `arch.py:314` 이 이름을 정렬해서 해싱한다.
    순서가 깨져도 digest 는 그대로고 테스트도 통과한다. 34 개 이름을 순서까지 문자열로
    대조하는 것 하나뿐이다.
  - **fingerprint 은 본문 무결성도 증명하지 않는다.** `structure_of` 는 tool 을 부르지 않고
    이름과 `args` 만 읽는다. 실제로 `PendingCapture` import 가 빠진 상태에서도 fingerprint
    은 `27f13c92130c` 로 멀쩡했고, `tests/test_qa_capture.py` 네 개가 그것을 잡았다. 본문은
    테스트와 자유 이름 점검이 지킨다.
  - 조립에서 tool 하나를 빠뜨리면 그때는 fingerprint 가 움직인다. 개수까지 함께 대조한다.
  - `_answer` 와 `_run` 을 메서드로 옮기면서 `state` 를 쓰는 자리를 놓치면 화면이 두 번
    실리거나 사라진다. 두 본문은 글자 그대로 옮기고 첫 줄에서 이름만 되묶었다.
- **Rollback steps:** `git revert`. 런타임 상태도 스키마도 건드리지 않으므로 되돌리는 데
  따로 할 일이 없다.

## Review 이력

1 차(fast·medium)에서 받은 것: 상수 줄 번호 정정, `*_tools.py` 이름 규칙, tool 안의 중첩
헬퍼(`refused`)도 함께 옮긴다는 명시, import cycle 근거 정정, `render_closing_asks` 재수출
철회, worktree 검증 명령 수정.

2 차(heavy)에서 받은 것과 처리:

- **fingerprint 이 순서를 지켜 준다는 서술이 거짓** — 정정하고, 순서 기준선 34 개를 문서에
  박고, 검증을 문자열 대조로 바꿨다.
- **모듈별 자유 이름 점검을 넣어라** — 넣었고, `PendingCapture` 누락을 잡았다.
- **되묶기를 고정 2 줄로 하면 죽은 이름이 생긴다** — 모듈마다 쓰는 것만 되묶도록 바꿨다.
- **700 줄 기준과 "지식을 나누지 않는다"는 동시에 성립하지 않는다** — 아래 참고.

## 철회한 반려

1 차 리뷰의 "지식 tool 을 read 와 write 로 나눠라"를 처음에는 반려했다. 근거는 나누면 tool
순서가 바뀐다는 것이었고, 그 근거 자체는 지금도 맞다. 순서는 `app/qa/run_config.py:166` 이
저장하고 모델도 그대로 받는다.

반려를 철회한 이유는 두 가지다. 하나는 나누지 않은 `knowledge_tools.py` 가 718 줄로 나와서
이 작업이 스스로 세운 700 줄 기준을 넘었다는 것이다. 다른 하나는 순서를 지키면서 나누는
길이 "조립부에서 다시 섞기" 말고도 있었다는 것이다. `knowledge_tools.py` 가

```python
search_knowledge, expand_knowledge = build_knowledge_read_tools(ctx)
return [search_knowledge, *build_knowledge_write_tools(ctx), expand_knowledge]
```

한 줄로 순서를 봉인한다. 순서 상식이 최상위 조립부가 아니라 지식 모듈 안에 남으므로,
반려 사유로 들었던 형태가 아니다. 나눈 뒤 이름 34 개의 순서는 기준선과 완전히 같다.

## 유지한 반려

**builder 첫 줄의 되묶기를 나중에 `ctx.` 로 인라인해라** (1 차 medium 리뷰). 되묶기는 이번
작업의 최종 형태로 둔다. 인라인하면 이 diff 가 순수 이동이 아니게 되고, 리뷰어가 "옮기기만
했다"를 눈으로 확인할 수 있다는 이 작업의 유일한 안전장치를 잃는다. 되묶기 줄은 그 아래
tool 이 무엇을 closure 로 잡는지 먼저 말해 주는 머리말로도 읽힌다. 2 차 리뷰의 지적대로
모듈마다 쓰는 이름만 적으므로 죽은 이름은 없다.

## Rebase

작업 도중 develop 이 `836081f` 에서 `f82c597` 로 움직였다. #151 이 `app/agents/qa/capability.py`
를 새로 넣고 `tools.py` 에 368 줄을 더했다 — capability tool 셋(`record_capability_verdict`,
`record_new_capability`, `list_scene_capabilities`)과 그 헬퍼 다섯, `QaRunState` 의 새 필드
셋과 `remember_dispatch`, 그리고 `_run` 이 보낸 조작을 기록하는 세 줄이다.

rebase 는 `tools.py` 에서 modify/delete 로 충돌했고, 삭제를 유지한 뒤 #151 의 delta 를 새
package 로 나눠 넣었다.

- `QaRunState` 의 새 필드 셋과 `remember_dispatch` → `state.py`
- `_run` 의 `state.remember_dispatch(actions)` 세 줄 → `ToolContext.run`
- capability 블록 316 줄 → 새 모듈 `capability_tools.py`
- 조립 목록의 세 자리 → `__init__.py`, `build_screen_selector_tools` 와 `build_action_tools`
  사이

그래서 주제가 다섯에서 여섯으로 늘었다. rebase 후 fingerprint `e8e1d4764809` 와 이름 37 개
순서가 새 기준선과 일치한다.

## Open Questions

- 없음.
