# 2026-07-29 — QA 툴 정의를 `@tool`로 전환하고 툴 설명을 단일 출처로 만든다

- Date: 2026-07-29
- Jira: ARTEL-192
- Status: Implemented

## Goal

`app/agents/qa/tools.py`가 툴을 정의하는 방식과, 그 툴들에 대한 설명이 어디에 사는지를 정리한다.

1. `StructuredTool.from_function(coroutine=..., name=...)` 17줄을 `@tool` 데코레이터로 대체한다. 툴 이름은 함수 이름에서 나온다.
2. 런타임 상수가 들어가야 하는 툴 설명은 데코레이터의 `description=`으로 옮겨, 상수와 설명이 갈라지지 않게 한다. 지금 `MAX_CAPTURES_PER_RUN`은 거절 메시지에만 있고 `capture_screen` 설명에는 숫자가 없다.
3. 시스템 프롬프트(`app/prompts/qa_run/v2/system.md`)와 툴 docstring이 같은 규칙을 서로 다른 문장으로 두 번 말하는 구간을 정리한다. 프롬프트 파일은 불변이므로 `v3`을 새로 만든다.

## Non-goals

- 툴의 동작 변경. 시그니처, 반환 문자열, 채널로 나가는 프레임은 그대로 둔다.
- `parse_docstring=True` 도입. 현재 docstring은 산문형이라 Google 스타일 `Args:`로 전부 다시 써야 하고, 그건 이 작업의 범위가 아니다.
- 인자별 설명을 위한 pydantic `args_schema` 명시. 타입힌트 기반 자동 추론을 계속 쓴다.
- v1/v2 프롬프트 수정. 두 버전 모두 그대로 둔다.
- QA 외 다른 에이전트(scenario, game_context)의 프롬프트.

## Context / Constraints

**툴 정의.** `build_tools(channel, state, supports_vision)`는 팩토리다. 각 툴은 `channel`과 `state`를 캡처하는 클로저이므로, 데코레이터는 함수 안에서 적용되어야 한다. `@tool`은 async 함수에 붙으면 `coroutine` 슬롯을 채우므로 지금과 동작이 같다. 테스트는 전부 `tools[name].ainvoke({...})`로 호출하고 `tool.name`으로 색인하므로 인터페이스가 유지된다.

**설명의 출처.** langchain-core 1.5.1의 `tool()`은 `description=` 인자를 받는다(확인함). docstring과 달리 런타임 문자열이라 상수 보간이 가능하다.

**프롬프트 버전은 불변이다.** `test_prompts_v1_regression.py`가 v1을 바이트 단위로 고정하고, `test_qa_prompt_version.py::test_v2_adds_the_new_tools_and_keeps_v1_intact`가 v2를 v1의 상위집합으로 고정한다. 문구를 바꾸려면 `app/prompts/qa_run/v3/`을 새로 만들어야 한다. 역할 파일은 버전마다 다 있어야 하므로 `system.md`와 `vision_directive.md` 둘 다 필요하다.

**기본 버전은 최신이다.** `resolve_version`은 설정이 비어 있으면 가장 높은 번호를 고른다. v3을 만드는 순간 새 실행의 기본값이 v3이 된다. `test_the_default_qa_version_is_v2`가 이걸 잡아내므로 같이 갱신한다. v2는 요청의 `prompt_version="v2"`로 계속 도달 가능하다 — A/B가 이 구조의 목적이다.

**중복 구간.** v2 `system.md`의 문단 ↔ 툴 docstring:

| 프롬프트 문단 | 중복 대상 |
|---|---|
| `vision_directive.md` 전체 | `capture_screen` docstring |
| hold/release 문단 | `hold_mouse_button`, `hold_key`, `release_*`, `drag_pointer` |
| pause/resume 문단 | `pause_game_time`, `resume_game_time` |
| 좌표 VERBATIM 문장 | `move_pointer`, `drag_pointer` |
| `wait_for_operator` 문단 | `wait_for_operator` |
| `press_key`는 타깃이 필요 없다 | `press_key` |

**옮길 수 없는 것.** 툴 하나가 소유하지 않는 규칙은 시스템 프롬프트에 남는다: 1~5번 루프 순서, `thought`/`step` 규약, 씬 출력 포맷(`@ x,y wxh`, `on screen:` 섹션), 운영자 메시지가 툴 결과 뒤에 붙는다는 사실, `{language_directive}`, "id를 지어내지 마라".

**분리 기준.** 이 툴만의 불변식 → 툴 설명. 툴 사이의 순서·선택·환경 형식 → 시스템 프롬프트. 시스템 프롬프트에는 한 줄 요약만 남기고 상세는 툴 설명이 든다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료. `app/agents/qa/tools.py`, `app/agents/qa/runner.py`, `app/prompts/qa_run/v2/*`, `tests/test_qa_tools.py`, `tests/test_qa_capture.py`, `tests/test_qa_prompt_version.py`, `tests/test_prompts_v1_regression.py` 확인함.

- [x] **Step 1a: `@tool` 전환** — `app/agents/qa/tools.py`
  - `StructuredTool` import를 `tool`, `BaseTool`로 교체. `build_tools` 반환 타입은 `list[BaseTool]`.
  - 툴로 노출되는 각 async 함수에 `@tool` 부착. 내부 헬퍼 `_run`은 툴이 아니므로 건드리지 않는다.
  - 파일 끝 `tools = [...]` 리스트는 데코레이트된 이름을 그대로 담는다. `name=` 인자 17개 소멸.
  - `supports_vision` 분기는 유지: `capture_screen`은 조건부로만 리스트에 들어간다.

- [x] **Step 1b: 설명 상수화** — `app/agents/qa/tools.py`
  - `capture_screen`은 `@tool(description=...)`로 바꾸고, 본문 문자열에 `MAX_CAPTURES_PER_RUN`을 보간해 한도를 설명 안에서 밝힌다. 함수 docstring은 짧은 개발자용 주석으로 남긴다.
  - 나머지 툴은 docstring을 그대로 설명으로 쓴다. 상수가 없는 설명을 문자열 상수로 옮기는 것은 이득 없이 코드만 늘린다.

- [x] **Step 1c: 툴 설명 보강** — 시스템 프롬프트에서 걷어낼 규칙 중 툴 자체의 불변식은 해당 docstring이 이미 말하고 있는지 확인하고, 부족하면 채운다. 특히 hold/release 짝과 pause/resume 짝은 "verdict 전에 풀어라"가 docstring에 남아야 한다.

- [x] **Step 2a: 프롬프트 v3 생성** — `app/prompts/qa_run/v3/system.md`, `app/prompts/qa_run/v3/vision_directive.md`
  - frontmatter `version: v3`, `note`에 v2 대비 변경 이유, `placeholders: [vision_directive, language_directive]`.
  - 위 중복표의 문단을 한 줄 요약으로 축약. 유지 항목은 그대로.
  - `vision_directive.md`는 `capture_screen` 설명과 겹치는 부분을 덜어내고, "언제 화면을 봐야 하는가"라는 판단 기준만 남긴다.

- [x] **Step 2b: 테스트 갱신**
  - `tests/test_qa_prompt_version.py::test_the_default_qa_version_is_v2` → v3 기대값으로 수정하고 이름도 맞춘다.
  - v3이 v2의 규칙을 잃지 않았음을 보는 테스트 추가: 축약된 문단이 다루던 툴 이름이 v3 프롬프트나 해당 툴 설명 중 어딘가에는 반드시 남아 있어야 한다.
  - `@tool` 전환 후에도 `build_tools`가 같은 이름 집합과 같은 인자 스키마를 내놓는지 보는 테스트 추가.

- [x] **Step 3: Rollout / Rollback** — 플래그 없음. 롤백은 `QA_PROMPT_VERSION=v2`로 환경변수 고정, 또는 커밋 revert.

## Validation

- **Commands to run:**
  - `python -m pytest`
  - `python -c "from app.prompts import validate_prompts; validate_prompts()"`
- **Expected output:** 전체 통과. v1 회귀 테스트가 바이트 단위로 계속 통과해야 한다. 프롬프트 검증은 v3의 frontmatter `placeholders`가 본문과 정확히 일치할 때만 통과한다.

## Risks & Rollback

- **Risks:**
  - 시스템 프롬프트 축약은 에이전트 행동 변화다. 모델은 시스템 프롬프트를 정책으로, 툴 설명을 호출 직전 참조로 읽는다. 중복 제거가 특정 규칙의 강도를 낮출 수 있다 — 특히 "잡은 것은 verdict 전에 놓아라"처럼 어겼을 때 이후 스텝 전부를 오염시키는 규칙.
  - 그래서 축약이지 삭제가 아니다. 각 규칙은 프롬프트에 한 줄로 남고 상세는 툴 설명이 든다.
  - v3이 자동으로 기본값이 되므로, 머지 즉시 새 실행에 적용된다. 실측 비교는 `prompt_version`으로 v2와 나란히 돌려서 한다.
- **Rollback steps:** `QA_PROMPT_VERSION=v2` 설정, 또는 커밋 revert. 툴 코드 전환은 동작 동일이라 단독 롤백 대상이 아니다.

## Open Questions

- 축약 강도: 한 줄 요약 유지로 결정했다. 결과는 렌더된 시스템 프롬프트 4224자 → 3862자(-9%), 문단 12 → 11. 토큰 절감이 목적이 아니라 드리프트 원인 제거가 목적이므로 이 정도로 둔다.
- `tests/test_agents_scenario.py::test_scenario_agent_passes_trace_config_to_runnable`는 이 작업 이전부터 실패한다(깨끗한 `develop`에서 확인). 이 변경과 무관하며 손대지 않았다.
