# 2026-08-11 — QA 에이전트에 축·버튼 입력 도구 추가

- Date: 2026-08-11
- Jira: ARTEL-298 (Epic ARTEL-11 [Backend] Agent 서버 개발)
- Branch: `feat/qa-에이전트에-축-버튼-입력-도구-추가-ARTEL-298`
- Status: Draft

## Goal

SDK가 추가한 `set_axis`·`set_button` 액션([ARTEL-292](https://artel-asm.atlassian.net/browse/ARTEL-292), [artel-sdk#53](https://github.com/project-artel/artel-sdk/pull/53))을 QA 에이전트가 부를 수 있게 한다. 도구를 붙이는 것에 그치지 않고, 축을 읽는 게임을 만났을 때 **에이전트가 그것을 알아내고 다음 실행에 물려주는 경로**까지 만든다.

## Non-goals

- 게임이 쓰는 축 이름 자동 발견. 레거시 Input Manager에 축 목록을 조회하는 런타임 API가 없어 SDK가 알려줄 수 없다. 에이전트가 Unity 기본 이름(`Horizontal`·`Vertical`·`Jump`)부터 시도하고 결과를 지식으로 남긴다
- `press_key`에 대응하는 축 원샷 도구. 축은 유지 상태라 누르고 떼는 것이 의미 있는 단위다
- SDK 쪽 변경. ARTEL-292에서 끝났다
- 프롬프트 의미 검증 자동화. `2026-08-06-lock-released-prompt-versions.md`의 Non-goals 그대로

## Context / Constraints

**`hold_key`가 닿지 못하는 곳이 있다.** SDK의 `ArtelInput`은 `Input.GetKey`를 가상화한다. 게임이 `Input.GetAxis("Horizontal")`을 읽으면 엔진이 Input Manager 바인딩과 **실제** 키 상태로 축을 계산하므로 가상 키가 보이지 않는다. `hold_key("D")`는 성공으로 응답하고 캐릭터는 움직이지 않는다.

**에이전트는 어느 쪽인지 알 수 없다.** 게임 코드를 보지 못하고, 씬 스캔도 입력 API를 보고하지 않는다. 알아내는 방법은 하나뿐이다 — 해보고 화면이 변하는지 본다.

**그래서 이 작업의 실질은 도구가 아니라 프롬프트다.** 도구만 추가하면 에이전트는 존재는 알아도 언제 쓸지 모른다. ARTEL-292가 지적한 조용한 실패가 그대로 남는다.

**전략: 폴백 + 지식 기록.** 키를 먼저 쓰고, 화면이 변하지 않으면 축으로 재시도하고, 어느 쪽이 먹혔는지 `record_knowledge`로 남긴다. 다음 실행의 `search_knowledge`가 그것을 돌려주면 왕복이 사라진다. 게임당 한 번만 헤매면 된다.

이 전략이 쓸 기계장치는 **이미 다 있다**. v9가 `## The knowledge base` 절에서 `record_knowledge`·`link_knowledge`·`UI` 태그·`LEADS_TO` 관례를 세워뒀고, 화면 지도가 "여러 도구에 걸친 작업 습관은 시스템 프롬프트가 맡는다"는 선례다. 축 판별도 같은 성격이다 — `hold_key`·`set_axis`·`record_knowledge`에 걸쳐 있어 어느 도구 설명에도 집이 없다.

**프롬프트 버전 제약 둘.**

1. **릴리즈된 버전은 못 고친다.** `app/prompts/lock.py` + `tests/test_prompts_lock.py`가 body sha256으로 잠근다. v9에 문단을 넣으면 CI가 빨개진다. **v10을 판다.**
2. **버전 추가는 lock 갱신을 동반한다.** `python -m app.prompts.lock --write` 후 커밋. 손으로 JSON을 고치면 잠금이 무의미해진다.

**`resolve_version`은 최고 번호 디렉터리를 돌려준다.** v10을 만드는 순간 `qa_prompt_version`을 고정하지 않은 모든 실행이 v10으로 옮겨간다. 그래서 프롬프트와 도구가 **같은 변경에 실려야** 한다 — 없는 도구를 가리키는 프롬프트는 에이전트에게 헛것을 잡게 한다. `test_the_default_qa_version_is_v9`도 같이 갱신해야 한다.

**`v10` 정렬은 안전하다 (확인 완료).** `available_versions()`는 디렉터리 이름에서 숫자를 뽑아 정렬하고, docstring이 "``v10`` is newer than ``v2``, which a lexical sort gets backwards"라고 명시한다. 이 작업이 v10을 만드는 첫 케이스지만 밟을 함정은 아니다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 일부 완료
  - ~~버전 정렬 확인~~ 완료. 숫자 기준이라 `v10`이 최신으로 인식된다
  - `roles_in()` 확인 완료. 디렉터리의 `.md` stem 전부를 role로 잡으므로, v10에 `system.md`와 `vision_directive.md` 둘 다 있어야 v9와 role 구성이 어긋나지 않는다
  - 도구 등록 지점: `app/agents/qa/tools.py`의 `tools: list[BaseTool]`
  - `hold_key`/`release_key` 도구 모양과 `_run` 헬퍼 시그니처
  - `JsonRpcAction`이 params를 그대로 싣는지 (SDK는 `[axisName, value]`를 받는다)
  - `tests/test_qa_tools.py`의 `make()`·`answer()` 헬퍼로 도구 프레임을 어떻게 검사하는지

- [ ] **Step 1: 도구 두 개**
  - 파일: `app/agents/qa/tools.py`, `hold_key`/`release_key` 옆
  - `set_input_axis(step, axis_name, value, thought)` → `JsonRpcAction(method="set_axis", params=[axis_name, value])`
  - `set_input_button(step, axis_name, pressed, thought)` → `method="set_button", params=[axis_name, pressed]`
  - **이름은 `set_axis`가 아니라 `set_input_axis`.** 도구 이름은 에이전트가 읽는 어휘이고, `set_axis`만으로는 무엇의 축인지 안 보인다. 와이어의 메서드 이름은 SDK 것을 그대로 쓴다
  - 설명이 담아야 할 것 (ARTEL-192: 도구 호출 방법의 단일 출처는 도구 설명이다)
    - `axis_name`은 Unity Input Manager 축 이름이고 **대소문자를 구분한다**. `Horizontal`·`Vertical`·`Jump`가 기본
    - `value`는 -1~1. 범위 밖이면 SDK가 실패로 응답한다
    - 없는 축 이름도 실패로 응답한다 — 조용히 성공하지 않는다
    - **해제 책임**: `hold_key`와 같다. 스텝이 끝나기 전에 `set_input_axis(..., 0)` 또는 `set_input_button(..., False)`로 놓아야 한다. 안 놓으면 이후 모든 스텝이 축이 눌린 채로 돈다
  - `tools` 목록에 등록

- [ ] **Step 2: 프롬프트 v10**
  - `app/prompts/qa_run/v10/` 생성. `system.md`와 `vision_directive.md` 모두 필요하다 (`roles_in`이 두 role을 기대한다 — Step 0에서 확인)
  - `vision_directive.md`는 v9 그대로 복사. 시각 지침은 이 변경과 무관하다
  - `system.md`는 v9 본문 + `## State you set...` 절에 축 문단 추가
  - frontmatter `note`에 v9와 갈리는 이유를 적는다. v8의 note가 선례다
  - 문단이 말해야 할 것
    - `hold_key`가 닿지 못하는 게임이 있다는 사실과 그 증상 — 액션은 성공, 화면은 그대로
    - 폴백 순서: 키 먼저, 안 되면 축
    - **결과를 `record_knowledge`로 남긴다.** 이것이 문단의 요점이다. 남기지 않으면 매 실행이 같은 왕복을 반복한다
    - 축도 유지 상태라 해제 책임이 `hold_key`와 같다
  - **도구 호출 방법은 쓰지 않는다.** ARTEL-192대로 그것은 도구 설명의 몫이다. 프롬프트가 말할 것은 여러 도구에 걸친 습관뿐이다

- [ ] **Step 3: 테스트 갱신 (lock보다 먼저)**
  - `test_the_default_qa_version_is_v9` → `v10`
  - **순서가 중요하다.** lock을 먼저 갱신하면 음성 대조에서 무엇이 red를 냈는지 구분되지 않는다. 이 시점에 `test_every_prompt_on_disk_is_in_the_lock` **하나만** red인 것을 확인하고 지나간다 — lock이 실제로 새 버전을 잡는다는 증거다

- [ ] **Step 4: lock 갱신**
  - `python -m app.prompts.lock --write` 실행 후 커밋
  - **손으로 JSON을 고치지 않는다.** conflict가 나도 `--write` 재실행으로 푼다

- [ ] **Step 5: 테스트 추가**
  - `tests/test_qa_tools.py` — 두 도구가 와이어에 싣는 프레임. 메서드 이름과 params 순서
  - `tests/test_qa_prompt_version.py`
    - v10이 v9 본문을 잃지 않았음 (기존 버전 테스트의 문단 상위집합 패턴)
    - v10이 축 지침과 `record_knowledge` 연결을 담고 있음
    - **v10의 role 구성이 v9와 같음.** `roles_in("qa_run","v10") == roles_in("qa_run","v9")`. `vision_directive.md`를 빠뜨리면 vision 실행이 런타임에 터지는데, 그 전에 잡는 것이 아무것도 없다. 조용히 깨지는 종류라 핀을 박는다
  - **도구 설명이 해제 방법을 명시하는지 핀 박기.** `test_v3_shortens_...`가 `hold_key`/`release_key` 쌍에 하는 것과 같은 검사. 축은 파트너 도구가 따로 없고 같은 도구에 0을 넣는 형태라, 설명이 그것을 말하지 않으면 놓을 방법이 어디에도 안 적힌다

- [ ] **Step 5: Rollout / Rollback**
  - 기능 플래그 없음. 도구 추가와 프롬프트 신규 버전
  - **기존 실행에 영향이 있다**: `qa_prompt_version`을 고정하지 않은 실행이 v10으로 옮겨간다. 이것이 프롬프트 버저닝의 설계된 동작이다
  - 롤백은 `git revert`. 단일 커밋으로 묶어 lock·프롬프트·도구가 함께 되돌아가게 한다

## Validation

- **Commands to run:**
  ```bash
  python -m pytest tests/test_qa_tools.py tests/test_qa_prompt_version.py tests/test_prompts_lock.py tests/test_prompts_loader.py
  ```
  ```bash
  python -m pytest
  ```
- **Expected output:** 전부 green. 착수 전 기준선을 먼저 잡아 기존 실패와 구분한다
- **기준선:** 착수 전 `python -m pytest` 실측 **499 passed**. 이후 실패는 전부 이 변경에 귀속된다
- **음성 대조:** Step 3까지 끝내고 Step 4(lock 갱신) 전에 `test_every_prompt_on_disk_is_in_the_lock` **하나만** red인 것을 확인한다. Step 3에서 기본 버전 테스트를 이미 v10으로 옮겨뒀으므로 그것과 섞이지 않는다
- **인터프리터:** `.venv/bin/activate`가 깨져 있다 (`pyvenv.cfg`의 `command`가 지금과 다른 경로를 가리킨다). `.venv/bin/python`을 절대 경로로 직접 부른다
- **검증하지 못하는 것:** 실제 게임 왕복. SDK PR #53이 develop에 병합되기 전에는 `set_axis`가 게임에 닿지 않는다. 도구가 싣는 프레임까지만 검증된다

## Risks & Rollback

- **Risks:**
  - ~~**`v10` 정렬.**~~ 해소. 숫자 정렬이 확인됐다
  - **프롬프트가 길어진다.** v9는 이미 길고, 문단 하나가 다른 지침의 주의를 가져간다. 짧게 쓰고 도구 설명과 중복하지 않는 것으로 관리한다
  - **폴백이 왕복을 한 번 낭비한다.** 축을 읽는 게임에서 첫 시도가 반드시 헛돈다. 지식 기록이 이것을 게임당 한 번으로 묶는 것이 설계의 대가다
  - **지식 기록이 실제로 재사용되는지는 이 작업으로 확인되지 않는다.** `search_knowledge`가 축 항목을 돌려주는지는 실제 실행에서만 보인다
  - **SDK 병합 순서.** #53 병합 전에 이 변경이 배포되면 도구는 있는데 SDK가 액션을 모른다. SDK가 `Unsupported method`로 실패 응답하므로 조용히 틀리지는 않는다

- **Rollback steps:** `git revert`. 저장된 상태나 마이그레이션 없음

## Plan Review

`.agents/skills/plan-review` 프로토콜. 서브에이전트가 이 세션에서 금지되어 스킬이 명시한 폴백대로 fast / medium / heavy 역할을 순차 자체 수행.

**must-fix (반영됨)**

- v10에 `vision_directive.md`를 빠뜨리면 vision 실행이 런타임에 터지는데 그 전에 잡는 것이 없었다 → role 구성 동등성 테스트를 Step 5에 추가.
- 음성 대조 순서가 실현 불가였다. lock 갱신 전 시점에는 기본 버전 테스트도 같이 red라 무엇이 잡았는지 구분되지 않는다 — `2026-08-06-lock-released-prompt-versions.md`가 같은 함정을 지적했던 그대로다 → 테스트 갱신(Step 3)을 lock 갱신(Step 4)보다 앞으로.

**should-fix (반영됨)**

- 도구 이름이 Open Questions에 미결로 남아 있는데 Step 1은 이름을 확정해 쓰고 있었다 → `set_input_axis`/`set_input_button`으로 확정. `hold_axis` 계열은 축이 -1~1 값이라 "hold"가 값을 표현하지 못해 기각.

**Rejected feedback**

- *`set_input_button`은 `set_input_axis(name, 1)`과 같으니 빼라.* 기각. SDK 쪽에서 같은 논의를 거쳐 두 액션을 유지하기로 정해졌다. 서버 도구만 하나로 합치면 에이전트가 쓰는 어휘와 SDK가 받는 액션이 어긋나고, 어긋남 자체가 다음 사람이 풀어야 할 문제가 된다.

**heavy 판정: PASS** — 범위는 도구 2개 + 프롬프트 1버전 + lock + 테스트. 되돌리기가 단일 커밋 revert.

## Open Questions

- v10 `note`에 SDK 이슈 키(ARTEL-292)를 남길지. v8 note가 ARTEL-257을 남긴 선례가 있어 남기는 쪽으로 간다

## Merge Log

- 2026-08-11 develop 머지: ARTEL-294(인용 보고, #65)가 먼저 머지되며 `qa_run/v10`을 가져갔다. 릴리즈된 버전은 고치지 않는다는 규칙(`app/prompts/lock.py`)에 따라 축 문단은 **v11**로 옮겼다 — v11 `system.md` = develop의 v10 본문 + 축 문단, `vision_directive.md`는 v10 그대로 복사.
- 이에 따라 이 문서에서 v10으로 적힌 계획은 모두 v11로 읽는다. 테스트도 `test_the_default_qa_version_is_v11`, `test_v11_teaches_the_axis_fallback_and_ties_it_to_the_knowledge_base`, `test_v11_defines_the_same_roles_as_v10`으로 옮겼고, lock은 `python -m app.prompts.lock --write`로 재생성했다.
