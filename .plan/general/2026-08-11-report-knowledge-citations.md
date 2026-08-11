# 2026-08-11 — QA Agent가 스텝 판정에 쓴 지식을 인용으로 보고한다

- Date: 2026-08-11
- Jira: ARTEL-294
- Status: Draft

## Goal

`report_step`이 그 판정에 실제로 쓴 지식을 함께 보고하게 한다. 검색으로 **무엇이 나갔는지**는
Orchestration이 이미 기록하지만(V27), 그중 무엇이 **행동에 반영됐는지**는 에이전트만 알고
있고 아무도 보고하지 않는다.

덤으로 `search_knowledge`가 이미 받고 있으면서 payload에 싣지 않던 `step`을 싣는다 —
그래서 `knowledge_usage.step`이 지금까지 항상 NULL이었다.

## Non-goals

- **액션 툴(`click_button`, `enter_text`, `press_key`, ...)의 인용.** 지식은 스텝 판단에
  작용하지 개별 클릭에 작용하지 않는다. 클릭마다 인용하게 하면 10클릭짜리 스텝의 항목이
  1클릭짜리보다 10배 유용해 보인다 — 지표가 유용성이 아니라 액션 수 가중치가 된다.
- 인용에 별도 좌표(case_id 등) 부여. 스텝에 `case_id`가 이미 매달려 있어(`QaStep`) 케이스
  단위 분석은 조인으로 접힌다.
- 인용률을 끌어올리는 프롬프트 압박. 아래 Risks 참조.

## Context / Constraints

### `report_step`이 맞는 자리인 이유

판정하는 곳에서 인용도 보고한다. 스텝 판정은 지식이 실제로 작용하는 유일한 지점이고,
`report_step`은 그 판정이 나가는 유일한 프레임이다.

### 지켜야 할 것

- **가드는 `state.knows_of()`다. `knowledge_seen`만 보면 안 된다.** 이웃으로 한 줄만 본
  항목(`knowledge_glimpsed`)도 행동의 근거가 될 수 있다. `knowledge_seen`을 요구하는 것은
  파괴적인 `update_knowledge` / `forget_knowledge`이고, 인용은 아무것도 파괴하지 않는다 —
  `tools.py`의 `knowledge_glimpsed` 주석이 그 경계를 이미 설명한다.
- **거부된 id 개수를 세서 프레임에 남긴다.** 환각 인용률 자체가 모델 비교 지표다. 조용히
  버리면 그 신호가 사라진다. STATUS payload에 실으면 Orchestration이 스텝 판정 STATUS를
  `qa_log`에 payload째 남기므로 별도 저장 경로 없이 기록된다.
- STATUS 프레임 payload에 실어 보낸다. **스텝 판정 STATUS는 `result=null`이라 런을 끝내지
  않는다** — Orchestration `routeStatus`의 2-scope 규칙(status 단어가 아니라 result로 가른다)을
  깨지 말 것.
- 기본값은 빈 리스트. 모델이 안 채워도 런이 정상 동작해야 한다.

### run_config 표식

Orchestration은 런이 끝날 때 미인용 행을 `cited=false`로 확정해야 하는데, "인용을 보고할 수
있었던 런"인지를 **추측이 아니라 기록으로** 갈라야 한다. `RunConfig`에
`citation_reporting: bool`을 더한다 — 세션 개설 응답으로 Orchestration에 그대로 전달되어
`qa_try.run_config`에 저장되고, 확정 질의의 술어가 된다. 이 필드가 없는 런(구버전 Agent)은
NULL로 남는다.

`RunConfig`가 맞는 자리인 이유: 그 모델은 "이 런이 무엇으로 돌았는지"의 기록이고 이미
`tools`와 `agent_fingerprint`처럼 **구조**를 담고 있다. 인용 보고 가능 여부도 구조의 성질이지
런 중에 바뀌는 상태가 아니다.

## Approach (Checklist)

- [ ] **Step 0: Recon** — `app/agents/qa/tools.py`(QaRunState, report_step, search_knowledge,
      expand_knowledge), `app/qa/envelope.py`(StatusPayload, KnowledgeSearchPayload),
      `app/qa/channel.py`, `app/qa/run_config.py`, `app/prompts/{loader,lock}.py`
- [ ] **Step 1: envelope** — `StatusPayload`에 `used_knowledge_ids: list[str]`와
      `rejected_knowledge_id_count: int` 추가. `KnowledgeSearchPayload` / `KnowledgeExpandPayload`에
      `step: int | None` 추가(optional — 구버전 Orchestration이 무시할 수 있어야 한다).
- [ ] **Step 2: channel** — `search_knowledge(..., step)` / `expand_knowledge(..., step)`이
      payload에 step을 싣는다.
- [ ] **Step 3: report_step** — `used_knowledge_ids: list[str] = []`. `knows_of()`로 거르고,
      거부된 개수를 세어 STATUS에 싣는다. 모델에게는 거부 사실을 결과 문자열로 알린다
      (조용히 버리면 모델이 인용이 기록된 줄 안다).
- [ ] **Step 4: run_config** — `RunConfig.citation_reporting`.
- [ ] **Step 5: 프롬프트** — `qa_run/v10` 추가(툴 시그니처와 지침이 바뀌었다). 인용을
      **압박하지 않는** 문구로 쓴다. `python -m app.prompts.lock --write`로 lock 재생성.
- [ ] **Step 6: 테스트** — `tests/test_qa_tools.py` 확장 + run_config 계약 테스트.

## Validation

- **Commands to run:**
  - `python -m pytest tests/test_qa_tools.py tests/test_qa_run_config_contract.py tests/test_prompts_lock.py tests/test_qa_prompt_version.py`
  - `python -m pytest`
- **Expected output:** 전부 통과. 특히
  - 기본값(인자 없음)으로 부르면 빈 리스트가 실리고 런이 정상 동작한다
  - `knowledge_seen`의 id를 인용하면 통과한다
  - `knowledge_glimpsed`(이웃 한 줄)만 본 id를 인용해도 통과한다
  - 본 적 없는 id는 거부되고 그 개수가 프레임에 남는다
  - 스텝 판정 STATUS는 여전히 `result=null`이다
  - `search_knowledge` / `expand_knowledge`가 step을 payload에 싣는다
  - 새 프롬프트 버전이 lock에 있고 이전 버전 본문이 안 바뀌었다

## Risks & Rollback

- **Risks:**
  - **인용은 자기신고다.** `knowledge_event`(관측)와 성격이 다르다. 모델이 빠뜨리므로
    **과소보고** 방향으로 치우친다 — 안전한 방향이지만, 인용률로 모델을 줄 세울 때
    "정직도" 차이가 섞인다. 코드 주석과 PR 본문에 남긴다.
  - **없애려고 프롬프트로 강하게 압박하지 않는다.** 압박하면 모델이 아무거나 인용하기
    시작하고, 그때는 편향의 방향조차 모르게 된다. 과소보고가 오염보다 낫다.
  - 툴 시그니처 변경이 모델의 도구 호출 형식을 바꾼다 — 기본값이 있어 기존 호출은 그대로
    유효하다.
- **Rollback steps:** `git revert`. Orchestration은 필드가 오지 않으면 cited를 NULL로 두므로
  되돌려도 데이터가 깨지지 않는다.

## Open Questions

- 없음. Orchestration 쪽 짝은 ARTEL-293이고 배포 순서에 의존하지 않는다.
