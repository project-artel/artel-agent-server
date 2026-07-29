# 2026-07-29 — QA 에이전트에 지식 기록·삭제 도구를 붙인다

- Date: 2026-07-29
- Jira: ARTEL-189
- Status: Implemented

## Goal

QA 런이 알아낸 것을 남기고, 더 이상 사실이 아닌 항목을 지울 수 있게 한다. 지금 런은 아무것도 남기지 않는다 — 실행 중에 "구매 버튼은 소지금이 부족하면 비활성화된다"를 관측해도 런이 끝나면 사라지고, 다음 런이 처음부터 다시 알아낸다.

Orchestration 쪽 인입(ARTEL-188)은 이미 develop에 있다. 이 작업은 Agent 절반 — 도구 2종, 단방향 송신, 결과 문자열 — 이다. ARTEL-187이 만든 검색(`search_knowledge`)과 같은 자리, 같은 결을 따른다.

## Non-goals

- **`update_knowledge`를 만들지 않는다.** 이슈가 정한 범위 결정이다. 고치려면 지우고 다시 기록한다. Orchestration의 `KNOWLEDGE_UPDATE` 경로는 그대로 두되, 당분간 이 Agent에는 호출자가 없다.
- 되살리기 도구.
- 런 종료 시 자동 지식 추출. 명시적 도구 호출만 다룬다.
- Orchestration 구현(ARTEL-188에서 완료).
- 프롬프트 v4. 아래 "설명은 툴이 든다" 참조.

## Context / Constraints

**확정된 와이어 계약(`origin/develop`의 Kotlin을 직접 읽어 확인).**

| 방향 | 타입 | payload | 응답 |
|---|---|---|---|
| Agent → Orche | `KNOWLEDGE_CREATE` | `{tag, summary, description}` | **없음** |
| Agent → Orche | `KNOWLEDGE_DELETE` | `{knowledge_id}` | **없음** |

- `QaAgentInboundRouter.routeKnowledgeMutation`은 **Agent로 아무 프레임도 돌려보내지 않는다.** 성공은 무음이고, 거절(`KnowledgeMutation.Rejected`)과 저장 중 예외는 `appendError`로 `ORCHE_INTERNAL` ERROR **로그 행**이 되어 운영자 SSE 스트림으로 나간다. 소켓으로는 오지 않는다.
- 검색과 다른 점이 여기다. ARTEL-186의 `failSearch`는 Agent 세션으로 ERROR 프레임을 보내지만, mutation에는 그런 경로가 없다. **응답을 기다리면 성공한 호출까지 전부 타임아웃까지 매달린다.**
- `projectId`/`source`/`source_id`는 payload에 없다. 라우터가 런에서 도출한다(`qaTryId → gameInstanceId → projectId`, `source=QA`, `source_id=qa_try.id`). Agent가 프로젝트를 지목할 수 없는 것이 의도다.
- `knowledge_id`는 문자열이다(`@JsonProperty("knowledge_id")`). 64비트 정밀도 손실을 피하려 조회 응답도 문자열로 낸다.
- `tag`는 `CONTROL|RULE|OBJECTIVE|UI|MISC`. `KnowledgeService.createFromQaTry`가 모르는 태그와 빈 `summary`/`description`을 거절한다 — 그리고 그 거절은 돌아오지 않는다.

**이 설계의 급소.** 수정 도구가 없으므로 "고치기"는 삭제 후 생성 두 단계다. 삭제는 먼저 저쪽에 적용되고, 그다음 생성이 실패하면 지식이 그냥 사라진다. 소프트삭제라 복구는 가능하지만 아무도 그 일이 있었는지 모른다. 방어는 네 겹이다.

1. `forget_knowledge`의 결과가 "고치려고 지웠으면 지금 곧바로 `record_knowledge`를 불러라"라고 말한다.
2. `record_knowledge`의 **모든** 실패 경로가 `render_missing_knowledge_warning`을 붙여, 밀린 삭제가 있으면 무엇이 지금 없어졌는지 이름을 대고 재시도를 요구한다.
3. 기록 상한은 **대체 쓰기에는 걸리지 않는다**. 상한은 런이 지식창고에 서술을 쏟아내는 것을 막으려고 있지, 수리의 나머지 절반을 막으려고 있는 것이 아니다. 걸리게 두면 상한 자체가 지식을 잃는 원인이 된다.
4. `BASE_TOOL_CALLS`가 기록 상한을 통째로 포함한다. 툴콜 예산이 모자라 대체 쓰기를 못 하는 런이 나오면 안 된다.

**보지 않은 항목은 지울 수 없다.** `search_knowledge`가 돌려준 hit의 id만 삭제 대상이 된다. Orchestration은 이 id를 실제 행으로 해석할 뿐 Agent가 그것을 읽었는지 알 방법이 없으므로, 검사는 이쪽에 있거나 아예 없다. 그래서 `render_hit`이 id를 출력하도록 바꾼다 — 안 그러면 이 규칙은 안전한 게 아니라 만족 불가능한 것이 된다.

**씬 뷰를 붙이지 않는다.** 두 도구 모두 `_run`도 `channel.look`도 타지 않는다. 화면을 바꾸지 않는 호출에 씬을 다시 실으면 ARTEL-180이 접어서 벌어 둔 컨텍스트를 도로 쓴다.

**설명은 툴이 든다.** ARTEL-192가 세운 단일 출처 방침. 187도 그렇게 했고 프롬프트를 건드리지 않았다. 여기도 같다 — 언제 기록하고 언제 지우지 말아야 하는지는 전부 툴 description에 있다. v3 프롬프트는 이미 "Each tool's own description says what it does ... Read it before reaching for the tool"이라고 말한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/agents/qa/tools.py`, `app/agents/qa/knowledge.py`, `app/qa/channel.py`, `app/qa/envelope.py`, `app/qa/service.py`, `app/agents/qa/runner.py`, `app/prompts/qa_run/v3/system.md`, `tests/test_qa_knowledge.py`, `tests/test_qa_tools.py`. Orchestration은 `origin/develop`의 `QaAgentInboundRouter.kt`, `KnowledgeDtos.kt`, `KnowledgeService.kt`.

- [x] **Step 1: 와이어 타입** — `app/qa/envelope.py`
  - `MessageType`에 `KNOWLEDGE_CREATE`, `KNOWLEDGE_DELETE`. `KNOWLEDGE_UPDATE`는 넣지 않는다 — 보낼 도구가 없으므로 이름만 있는 상수가 된다.
  - `KnowledgeCreatePayload{tag, summary, description}`, `KnowledgeDeletePayload{knowledge_id}`. Orchestration이 셋을 DTO 하나로 받는 것과 달리 둘로 나눈다: 이쪽에서 두 payload는 공유하는 필드가 없고, 하나로 합치면 전 필드가 optional이 되어 무엇이 필수인지가 주석으로만 남는다.

- [x] **Step 2: 단방향 송신** — `app/qa/channel.py`
  - `write_knowledge(message_type, payload)`. waiter 없음, correlation 없음. `search_knowledge` 바로 아래에 두어 비대칭이 눈에 보이게 하고, 왜 기다리지 않는지를 거기 적는다.

- [x] **Step 3: 상수·설명·렌더링** — `app/agents/qa/knowledge.py`
  - `MAX_RECORDS_PER_RUN = 5`, `MAX_FORGETS_PER_RUN = 2`. 삭제가 이 모듈에서 가장 작은 숫자다 — 되돌리기 가장 어렵고 아무도 안 보는 행위다.
  - `RECORD_KNOWLEDGE_DESCRIPTION`, `FORGET_KNOWLEDGE_DESCRIPTION`. 상한과 태그를 보간한다.
  - `render_entry_label`, `render_missing_knowledge_warning`.
  - `render_hit`에 id 추가.

- [x] **Step 4: 도구** — `app/agents/qa/tools.py`
  - `record_knowledge(step, thought, tag, summary, description)`, `forget_knowledge(step, thought, knowledge_id)`.
  - `QaRunState`에 `knowledge_records_attempted`, `knowledge_forgets_attempted`, `knowledge_seen`, `knowledge_deleted_unreplaced`.
  - `search_knowledge`가 hit의 id를 `knowledge_seen`에 남긴다.
  - 카운트는 시도 기준이다. 응답이 없으므로 성공은 셀 수 있는 것이 아니다.
  - 송신 실패는 값으로 돌아온다. `QaCancelled`만 통과시킨다.

- [x] **Step 5: 툴콜 예산** — `app/agents/qa/runner.py`
  - `BASE_TOOL_CALLS = 10 + MAX_SEARCHES_PER_RUN + MAX_RECORDS_PER_RUN + MAX_FORGETS_PER_RUN` (16 → 23). BASE는 런 길이와 무관한 몫이라는 187의 분리 기준 그대로.

- [x] **Step 6: 테스트** — `tests/test_qa_knowledge.py`, `tests/test_qa_tools.py`
  - 프레임 모양, 대기하지 않음, 타임라인, 못 본 id 거절, 두 번 삭제 거절, 상한, 송신 실패.
  - **"삭제 성공 → 기록 실패"** 전용 섹션. 기록이 거절되는 세 경로 전부와 송신 실패 경로.
  - 대체 쓰기가 상한에 걸리지 않는 것.
  - ARTEL-180 회귀(씬 뷰 없음).
  - 런너 레벨 E2E: 검색 → 삭제 → 기록 → 판정 → 종료.

- [x] **Step 7: Rollout / Rollback** — 플래그 없음. 되돌리려면 커밋 revert. 도구가 사라지면 에이전트는 그냥 못 쓸 뿐이고 다른 도구에 영향이 없다.

## Validation

- **Commands to run:** `uv run --extra dev python -m pytest`
- **Expected output:** 기준선 `1 failed, 256 passed`(베이스 `1405b26`에서 확인), 변경 후 `1 failed, 280 passed`. 실패는 이 작업 이전부터 있던 `tests/test_agents_scenario.py::test_scenario_agent_passes_trace_config_to_runnable` 하나뿐이며 ARTEL-195(PR #34)가 다룬다.

## Risks & Rollback

- **Orchestration과의 실제 왕복은 검증하지 못했다.** 프레임 모양은 `origin/develop`의 Kotlin DTO와 라우터를 읽어 맞췄고, 테스트는 그 계약을 흉내 낸 가짜 상대와 돈다.
- **성공을 확인할 방법이 없다.** mutation은 응답이 없으므로, 이 서버는 "프레임이 나갔다"까지만 말할 수 있고 "저장됐다"는 말할 수 없다. 저쪽에서 거절되면 그 사실은 운영자 타임라인에만 남고 Agent는 모른다. 결과 문자열이 그 한계를 그대로 말하도록 썼다.
- **삭제 판단은 결국 모델이 한다.** 상한과 "본 것만 지울 수 있다"가 실제 방어선이고, "한 번 어긋났다고 지우지 마라"는 설명이 지켜지기를 바라는 쪽이다. 실제 런을 눈으로 보는 검증은 남아 있다.
- **Rollback steps:** 커밋 revert. 프롬프트를 건드리지 않았으므로 프롬프트 버전 되돌림은 필요 없다.

## Open Questions

- 지식 쓰기가 `qa_log`에 남지 않는다. 라우터가 mutation을 로그 행으로 만들지 않기 때문이다(거절될 때만 ERROR 행이 생긴다). 런 타임라인에는 `thought`만 남는다. 감사 흔적은 `knowledge` 테이블의 `source_id`/`deleted_by_qa_try_id`에 있으므로 당장은 충분하다고 본다.
- `MAX_FORGETS_PER_RUN = 2`는 근거 없는 첫 숫자다. 실제 런을 보고 조정한다.
