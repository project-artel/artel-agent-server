# 2026-07-30 — TestScenario 저작 챗봇을 런 스코프·복수 시나리오·케이스 연결로 확장

- Date: 2026-07-30
- Jira: ARTEL-206 (TestScenario 구조 변환으로 인한 Agent 호출 방식 리팩토링)
- Status: Draft
- Repos: `artel-agent-server`(주) + `artel-orchestration-server`(계약/저장/검색 프레임 핸들러) + `artel-home`(FE, 대부분 ARTEL-198에서 완료)

## Goal

3-tier(TestRun → TestScenario → TestCase) 구조에 맞춰, 시나리오 저작 LLM 챗봇을 **런 스코프**로 확장한다. 사용자가 런 대시보드에서 자연어로 요청하면:
1. Agent가 자연어를 해석하고
2. **기존 TestCase를 검색**(케이스는 Orche에만 있음)해서
3. 적합하게 매핑하며, 필요 시 **여러 개의 시나리오로 분해**하여
4. Orche가 각 시나리오를 생성 + 케이스를 연결(`test_scenario_case`) + 런에 추가(`test_run_scenario`)한다.

## Non-goals

- **TestCase "생성"은 이 작업 범위 아님.** 별도 이슈 **ARTEL-177**(담당: 정의진, 진행 중) — "기능 테스트 명세를 CSV로 출력하는 Agent". 본 작업(206)은 **검색·연결(매핑)만** 한다. 케이스는 이 챗봇 실행 전에 이미 존재해야 함(177 산출 or 사용자 수동).
- QA 실행 에이전트(`app/agents/qa/`)는 건드리지 않음. 대상은 **시나리오 저작 에이전트**(`app/agents/scenario/`).
- 임베딩 기반 케이스 의미검색은 **후속**(초기엔 텍스트 검색). 단 knowledge가 이미 Vector+임베딩을 쓰므로 확장 여지는 열어둠.

## Context / Constraints

### 현재 저작 계약 (단일 시나리오) — 확인 완료
오케스트레이션 `TestScenarioAgentService` 기준:
- `POST {agent}/sessions {user_input, unity_context, game_context, model, locale}` → `{session_id}`
- `WS {agent}/sessions/{session_id}`: 연결 즉시 Agent가 첫 result 전송 → 후속 `{type:"turn", user_input, draft?, model?}` → 종료 `{type:"close"}`
- 결과: `{type:"result", message, scenario}`, `scenario = ScenarioDraft{title, description, steps[]}`, `ScenarioStep{step,title,state,action,expected}`

### 핵심 구조 사실 (Explore 매핑 완료)
- 저작 에이전트(`app/agents/scenario/agent.py:39-57`)는 **툴 없는 단일 structured LLM 1콜** → `ScenarioAgentResult{message, scenario}`(`schemas.py:65-67`).
- 세션 = 불투명 `uuid4`(`app/sessions/service.py:39`), 저작은 **초안 반환만**, **저장은 Orche 담당**(`project.md`: "Persistent data: None yet").
- **TestRun/TestCase/복수 시나리오 개념 코드 전역에 0.**
- 프롬프트 스코프 단수: `app/prompts/scenario/v1/system.md:6` "a game QA test scenario".
- API/WS: `app/api/sessions.py`(POST:64-76, WS:91-151, result 이벤트:52-57).

### 케이스 검색 = WS 프레임 왕복 (HTTP 역호출 아님) — 결정적 발견
knowledge 검색이 완벽한 청사진:
- 툴 → `channel.search_knowledge()` → **WS로 `KNOWLEDGE_SEARCH` 프레임 전송**(`app/qa/channel.py:201-235`), `messageId` 상관관계 + `KnowledgeSearchResult` 프레임 대기(waiter+timeout). 실패 3분기(payload/refusal(`KnowledgeSearchFailed`)/silence(None)).
- **Agent는 Orche 주소·인증을 모름.** 이미 열린 WS 위에서 프레임만 주고받고, **실제 Vector 검색은 Orche가 수행**해 결과 프레임 반환.
- → 케이스 검색도 **동일 패턴 복제**: `TEST_CASE_SEARCH` 프레임 타입 신설. **Agent→Orche 역방향 HTTP 불필요, 무상태 원칙 유지.**
- **중요**: TS 저작 시에도 이미 Agent와 WS 연결이 열려 있음 → 그 WS 위에 케이스 검색 프레임을 얹으면 됨(별도 커넥션 불필요).

### 확정된 설계 결정
- **A안(기존 재사용): Agent는 "런 플랜"을 반환, 저장은 Orche.** (에이전트 무상태 유지, ARTEL-198에서 만든 `/cases`·`/scenarios` API 재사용)
- **케이스 접근 = WS 프레임 검색 툴.** Context 주입은 불가 판정(케이스는 점진 누적 + 첫 Import부터 대량 → 컨텍스트 폭발). knowledge와 동일 방식.
- **챗봇은 케이스 검색·연결만, 생성 안 함**(177 담당).

### 데이터 모델 정렬 이슈 (해소 필요)
- 저작 에이전트는 `payload.steps`(state/action/expected 인라인)를 만드는데, **새 3-tier FE는 TestCase 조합(category/title/precondition/expected/verificationStatus)을 시나리오 본문으로 사용**(`/cases`). 즉 기존 출력 스키마가 새 모델과 어긋남 → 새 출력은 **steps가 아니라 caseIds 참조**로 간다.

## Approach (Checklist)

- [ ] **Step 0: Recon (일부 완료)**
  - [x] 저작 계약/에이전트/세션/툴/3-tier 인지 여부 매핑 (Explore)
  - [x] `search_knowledge` 전송 방식 = WS 프레임(`app/qa/channel.py:201`) 확인
  - [ ] Orche 쪽 `KNOWLEDGE_SEARCH` 프레임 수신·Vector 검색 핸들러 위치 확인 (최근 커밋 "knowledge 벡터 검색과 QA WS 지식 질의 응답 추가", ARTEL-186). 케이스 검색 프레임 핸들러를 붙일 자리 확정.
  - [ ] 정의진(ARTEL-177)의 TestCase 스키마/인터페이스 확인 — 케이스 검색 결과가 실을 필드 합의 (현재 우리 `TestCase` 모델: category/title/precondition/expected/verificationStatus/lastVerifiedBuildId 기준 가정).

- [ ] **Step 1: 구현 — Agent (`artel-agent-server`)**
  - [ ] 세션 오픈 페이로드 확장: `run_id`(+ project_id) 스코프. (`app/api/sessions.py`, `app/sessions/*`)
  - [ ] `search_test_cases` 툴 추가 — `search_knowledge` 패턴 복제. 저작 에이전트를 툴 사용 가능 형태로(현재는 툴 없는 단일 콜 → 툴 루프 or 사전 검색 단계). `app/agents/scenario/`.
  - [ ] `channel`에 `search_test_cases()` + `TEST_CASE_SEARCH`/`TEST_CASE_SEARCH_RESULT` 프레임 타입·페이로드·waiter 추가. (scenario 세션용 channel이 있는지, 없으면 신설)
  - [ ] 출력 스키마 확장: `ScenarioAgentResult` → **복수 시나리오** `{message, scenarios:[{title, description, caseIds:[...]}]}`. steps 제거/deprecate. (`app/agents/scenario/schemas.py`)
  - [ ] 프롬프트 스코프: 단수 → "런 목표를 여러 시나리오로 분해, 각 시나리오에 검색된 케이스를 매핑". `app/prompts/scenario/v2/`(버전업).

- [ ] **Step 1b: 구현 — Orchestration (`artel-orchestration-server`)**
  - [ ] 세션 오픈을 런 스코프로: `run_id` 전달, 세션키 재설계(현 `userId:testScenarioId` → 런 기반).
  - [ ] **`TEST_CASE_SEARCH` WS 프레임 핸들러**: Agent 요청 수신 → 케이스 검색(초기 텍스트: title/category LIKE + status 필터, `listTestCases` 확장) → 결과 프레임 반환. knowledge 핸들러와 대칭.
  - [ ] **result reconcile**: `scenarios[]` 수신 → 각 시나리오 생성 + `PUT /test-scenario/{id}/cases`로 케이스 연결 + `PUT /test-runs/{runId}/scenarios`로 런 조합. (ARTEL-198 API 재사용)
  - [ ] `ScenarioStreamEvent`/DTO를 복수 시나리오로 확장, FE로 진행상황 중계.

- [ ] **Step 1c: FE (`artel-home`) — 대부분 ARTEL-198 완료, 잔여만**
  - [ ] 런 편집 셸의 챗봇이 런 스코프 세션을 열도록(자연어 → 복수 시나리오 생성 반영). 생성 결과가 시나리오 목록/Map에 반영·케이스 선택 상태 표시.
  - [ ] 케이스 0개 런 가드(이미 빈 런 셸 존재) + "먼저 케이스 필요" 안내.

- [ ] **Step 2: Tests**
  - [ ] Agent: `search_test_cases` 툴 단위테스트(프레임 송수신 mock), 복수 시나리오 출력 파싱/검증.
  - [ ] Orche: `TEST_CASE_SEARCH` 프레임 핸들러 + reconcile 통합테스트(시나리오 N개 생성 + 링크 + 런 조합).
  - [ ] 수동 e2e: 런 대시보드 → 자연어 → 케이스 검색 → 복수 시나리오 생성 → Map 확인.

- [ ] **Step 3: Rollout / Rollback**
  - [ ] Agent 계약 버전업(v2 프롬프트/스키마) — 하위호환 or 동시 배포. Orche/Agent 계약 변경은 함께 배포.
  - [ ] 롤백: 계약 되돌리기(단일 시나리오 v1 유지 경로).

## Validation
- **Commands to run:**
  - Agent: `python -m pytest`
  - Orche: `./mvnw test`
  - FE: `npm run build`
- **Expected output:** 각 스위트 통과 + 수동 e2e(자연어→복수 시나리오+케이스 연결) 동작.

## Risks & Rollback
- **Risks:**
  - 계약 대폭 변경(단일→복수, steps→caseIds) — Agent/Orche/FE 3자 동시 정합 필요.
  - ARTEL-177(케이스 생성)과의 스키마/경계 미확정 — 케이스 검색 결과 필드가 177 산출과 맞아야 함.
  - 케이스 검색 초기 텍스트 검색의 매핑 품질(자연어→케이스) 한계 → 임베딩 승격 필요 시점 판단.
  - WS 프레임 왕복이 LLM tool-calling 흐름에 끼어들 때의 타임아웃/상관관계 관리(knowledge 선례로 완화).
- **Rollback steps:** 계약 v1(단일 시나리오)로 복귀, 신규 프레임 타입 비활성.

## Open Questions
- **케이스 접근**: 초기 텍스트 검색으로 시작 확정? 아니면 처음부터 임베딩(177/184가 케이스 임베딩까지 하면)?
- **ARTEL-177 인터페이스**: 정의진의 TestCase 스키마/CSV 계약 확인 필요 — 검색 결과·연결 대상 필드 합의.
- **세션 스코프**: 완전 런 단위(한 세션에서 여러 시나리오 저작) vs 시나리오 단위 유지 + 케이스 연결만? (현 논의는 런 스코프 지향)
- **개별 생성 요청**: 사용자가 "이 시나리오 하나만" 요청하는 경우도 같은 런 세션에서 처리(N=1)로 통합 가능.
- **steps 필드 처리**: 완전 제거 vs deprecate 유지(구 데이터 호환).
</content>
