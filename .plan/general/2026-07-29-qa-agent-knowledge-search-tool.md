# 2026-07-29 — QA 에이전트에 지식 검색 도구를 붙인다

- Date: 2026-07-29
- Jira: ARTEL-187
- Status: Implemented

## Goal

QA 실행 에이전트가 런 도중 프로젝트 지식창고에 질문할 수 있게 한다. 지금 에이전트가 게임에 대해 아는 것은 시작 시 주입되는 시나리오 텍스트뿐이고, 도구 17개는 전부 관찰·조작·보고다. "골드가 모자라면 구매 버튼이 어떻게 되나"처럼 **화면만으로는 판정할 수 없는 규칙**을 물어볼 곳이 없다.

Orchestration 쪽 검색(ARTEL-186)은 이미 있다. 이 작업은 Agent 절반 — 도구, WS 왕복, 결과 렌더링 — 이다.

## Non-goals

- 지식 기록·수정·삭제 도구(ARTEL-189). `KNOWLEDGE_CREATE`/`UPDATE`/`DELETE` 인입은 Orchestration에 이미 있으나 송신부는 이 이슈가 만들지 않는다.
- Orchestration 검색 구현(ARTEL-186).
- 시나리오 생성 에이전트에 지식 주입.
- 프롬프트 v4. 아래 "설명은 툴이 든다" 참조.

## Context / Constraints

**확정된 와이어 계약(ARTEL-186 코드를 직접 읽어 확인).**

| 방향 | 타입 | 내용 |
|---|---|---|
| Agent → Orche | `KNOWLEDGE_SEARCH` | payload `{query, tags?, tag?, source?, limit?}` |
| Orche → Agent | `KNOWLEDGE_SEARCH_RESULT` | payload `{query, model, results[]}`, `correlationId` = 요청 `messageId` |
| Orche → Agent | `ERROR` | 같은 `correlationId`, payload `{message}` |

- 검색 범위(projectId)는 payload에 없다. 라우터가 `qaTryId → gameInstanceId → projectId`로 해석한다 — Agent가 프로젝트를 지목할 수 없는 것이 의도다.
- 실패는 전부 `ERROR` 프레임으로 온다. `KnowledgeSearchService`가 예외를 삼키지 않고, 라우터가 그것을 ERROR로 번역한다.
- **빈 결과는 오류가 아니다.** 백필이 비동기라 벡터가 아직 없는 것이 정상 상태다.
- 인바운드 `ERROR` payload에는 `code`가 없다(`objectMapper.createObjectNode().put("message", reason)`). 그래서 `ErrorPayload`로 검증하면 안 된다 — 그 모델은 `code`를 필수로 요구한다.
- `tag`는 `CONTROL|RULE|OBJECTIVE|UI|MISC`, `source`는 `DOCS|QA`. 라우터는 **모르는 토큰 하나에 요청 전체를 거절한다** — 조용히 무시된 필터는 "필터가 안 걸린 결과"로만 드러나 오류로 보이지 않기 때문이다.

**단수 `tag`를 쓴다.** Orchestration은 `tags`(복수)와 `tag`(단수)를 둘 다 받고 합집합으로 쓴다. 둘 중 하나를 골라야 한다.

- 도구는 태그를 **하나만** 노출한다. 한 질문은 한 주제다 — "구매 규칙"은 `RULE`이지 `RULE`이면서 `UI`가 아니다.
- 그러면 와이어도 단수여야 인자와 프레임이 같은 모양이 된다. 복수로 보내면 "비어 있음"과 "없음"을 리스트 길이로 구분하는 자리가 하나 더 생기고, 모델이 채울 스키마도 리스트가 된다.
- 결과 항목(`KnowledgeSearchHit.tag`)도 단수다. 에이전트가 결과에서 읽은 값을 그대로 다음 검색의 필터로 넘길 수 있다.

**씬 뷰를 붙이지 않는다.** 조회는 화면을 바꾸지 않으므로 씬을 다시 실을 이유가 없고, ARTEL-180(`fold_stale_scenes`)이 접어서 벌어 둔 컨텍스트를 도로 쓰게 된다. 그래서 이 도구는 `_run`도 `channel.look`도 거치지 않는다 — 그 둘이 씬을 싣는 유일한 경로다.

**검색 결과는 접히지 않는다.** `fold_stale_scenes`는 씬 뷰 마커 사이만 접는다. 지식 결과 텍스트는 런이 끝날 때까지 컨텍스트에 남는다. 그래서 양 제한이 도구 쪽에도 필요하다: 항목 수(`RESULT_LIMIT`)와 항목당 본문 길이(`MAX_DESCRIPTION_CHARS`) 둘 다.

**설명은 툴이 든다.** ARTEL-192가 "툴 설명이 단일 출처"를 방침으로 세웠다(`.plan/general/2026-07-29-tool-descriptions-as-single-source.md`). 이슈 본문은 그 이전에 쓰였고 "시스템 프롬프트에 한 문단 추가"를 요구하지만, 방침을 따라 **프롬프트를 건드리지 않는다**. 언제 쓰고 언제 쓰지 말아야 하는지는 전부 툴 설명에 넣는다. v3 프롬프트는 이미 "Each tool's own description says what it does ... Read it before reaching for the tool"이라고 말하고 있다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/qa/channel.py`, `app/qa/envelope.py`, `app/qa/service.py`, `app/agents/qa/tools.py`, `app/agents/qa/vision.py`, `app/agents/qa/context.py`, `app/agents/qa/runner.py`, ARTEL-186의 `QaAgentInboundRouter.kt` / `KnowledgeSearchDtos.kt` / `KnowledgeTag.kt` / `KnowledgeSearchProperties.kt` 확인함.

- [x] **Step 1: 와이어 타입** — `app/qa/envelope.py`
  - `MessageType`에 `KNOWLEDGE_SEARCH`(송신), `KNOWLEDGE_SEARCH_RESULT`(수신) 추가. 이름은 Orchestration의 `SUPPORTED_TYPES`/`sendToAgent` 문자열과 정확히 같다.
  - 송신 payload `KnowledgeSearchPayload{query, tag?, limit}`.
  - 수신 payload `KnowledgeSearchHit`, `KnowledgeSearchResultPayload`. 필드에 전부 기본값을 준다 — 검증에 걸려 프레임이 버려지면 기다리던 도구가 타임아웃까지 매달리고, 필드 이름 하나 바뀐 값을 런이 20초로 치른다.

- [x] **Step 2: 채널 왕복** — `app/qa/channel.py`
  - `KNOWLEDGE_SEARCH_TIMEOUT_SECONDS = 20.0`. 액션(30s)보다 짧다: 지식 조회는 진행이 아니라 맥락을 사는 호출이라, 게임 왕복만큼 기다려 줄 이유가 없다.
  - `search_knowledge(...)` → `KnowledgeSearchResultPayload | KnowledgeSearchFailed | None`. 세 상태를 타입으로 가른다: 결과 / Orchestration이 거절 / 아무것도 안 옴. `dispatch_actions`와 같이 예외가 아니라 값으로 돌려준다 — 무엇을 할지는 에이전트가 정한다.
  - `on_knowledge_search_result`, `on_error`. 둘 다 `correlationId`를 대조한다.
  - `on_cancel`이 이 waiter도 깨운다.

- [x] **Step 3: 인입 라우팅** — `app/qa/service.py`
  - `deliver`가 `KNOWLEDGE_SEARCH_RESULT`와 `ERROR`를 받는다. `ERROR`는 짝이 없어도 `True`를 돌려준다 — 프로토콜상 정당한 프레임이라 "Unsupported inbound frame"으로 되받아치면 안 된다. 짝이 없으면 경고 로그만 남는다.

- [x] **Step 4: 지식 모듈** — `app/agents/qa/knowledge.py` (신규)
  - `vision.py`가 촬영 관련 상수·렌더링을 모아 두는 것과 같은 자리. 상한, 태그 집합, 도구 설명, 결과 렌더링.
  - `MAX_SEARCHES_PER_RUN = 6`. `MAX_CAPTURES_PER_RUN`과 같은 이유 — 판단 대신 검색을 반복하는 런은 데드라인에 아무것도 보고하지 못한 채 닿는다.
  - 도구 설명에 상한 숫자를 보간한다. 한도를 부딪혀서 배우는 에이전트는 이미 그것을 다 쓴 뒤다.

- [x] **Step 5: 도구** — `app/agents/qa/tools.py`
  - `search_knowledge(step, thought, query, tag=None)`.
  - 상한 → 태그 검증 → 카운트 증가 → 왕복 순서. 모르는 태그는 왕복도 예산도 쓰지 않고 허용값을 알려 준다(Orchestration이 조용한 필터를 거절하는 것과 같은 판단).
  - 카운트는 시도 기준이다. 성공만 세면 계속 실패하는 검색이 무한이 된다.
  - `QaRunState.knowledge_searches_attempted` 추가.

- [x] **Step 6: 툴콜 예산** — `app/agents/qa/runner.py`
  - `BASE_TOOL_CALLS` 10 → 16. 정확히 새 런 단위 상한(6)만큼 올린다. `BASE`는 런 전체가 쓰는 몫(첫 관찰, `finish_run`), `PER_STEP`은 스텝 작업의 몫이다. 그대로 두면 지식 검색이 스텝 작업 예산을 갉아먹는다.

- [x] **Step 7: 테스트** — `tests/test_qa_knowledge.py`(신규), `tests/test_qa_tools.py`, `tests/test_qa_service_deliver.py`

- [x] **Step 8: Rollout / Rollback** — 플래그 없음. 되돌리려면 커밋 revert. 도구가 사라지면 에이전트는 그냥 못 물어볼 뿐이라 다른 도구에 영향이 없다.

## Validation

- **Commands to run:** `uv run --extra dev python -m pytest`
- **Expected output:** 신규 테스트 포함 전부 통과. 기존 상시 실패
  `tests/test_agents_scenario.py::test_scenario_agent_passes_trace_config_to_runnable`
  는 이 변경 이전 `4d979c9`에서도 실패한다(확인함). 이 작업과 무관하며 손대지 않았다.

## Risks & Rollback

- **Risks:**
  - **Orchestration과의 실제 왕복은 검증하지 못했다.** 프레임 모양은 ARTEL-186의 Kotlin DTO와 라우터를 읽어 맞췄고, 테스트는 그 계약을 흉내 낸 가짜 상대와 돈다. 실제 통합은 두 서버가 같이 뜬 뒤에야 확인된다.
  - 에이전트가 매 스텝 검색을 부르면 툴콜 예산이 탄다. 막는 것은 두 겹이다 — 설명의 "언제 쓰지 말아야 하는지"와 `MAX_SEARCHES_PER_RUN`. 앞의 것은 모델이 지키기 나름이라 뒤의 것이 실제 방어선이다.
  - 검색 결과는 접히지 않고 런 끝까지 컨텍스트에 남는다. 최악은 6회 × 5항목 × 500자 ≈ 15KB. 씬 뷰 하나가 접히기 전 차지하던 양과 비슷한 규모라 감당 가능하다고 본다.
- **Rollback steps:** 커밋 revert. 프롬프트를 안 건드렸으므로 프롬프트 버전 되돌림은 필요 없다.

## Open Questions

- `source` 필터(`DOCS`/`QA`)는 노출하지 않았다. 지금 채워지는 출처는 `DOCS`뿐이고(런에서 배운 것을 쓰는 경로는 ARTEL-189), 고를 것이 하나면 인자는 모델이 틀릴 자리만 된다. 두 출처가 실제로 공존하면 그때 연다.
- `limit`도 노출하지 않았다. 개수는 컨텍스트 예산이지 에이전트가 조율할 것이 아니다.
