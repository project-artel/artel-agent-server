# 2026-07-21 — 시나리오 Agent 세션(REST+WS) 설계

- Date: 2026-07-21
- Jira: ARTEL-40
- Status: In Review

## Goal

테스트 시나리오 생성 agent를 **챗봇형 반복 개선(iterative refinement) 세션**으로 동작시키기 위한 설계를 확정한다.
사용자가 자연어로 요청하면 step 단위 테스트 플로우 초안과 대화 메시지를 생성하고, 사용자가 캔버스에서 초안을 수동 편집하며 대화를 이어가다 approve/decline으로 마무리하는 흐름을, **agent_server가 책임지는 범위 안에서** 정의한다.

이 문서는 설계 확정용이다. 최초 초안은 코드 변경을 포함하지 않았고, 이후 이 설계대로 구현이 반영되었다(아래 Approach 체크리스트 참조).

## Non-goals

- 프론트엔드 캔버스/대화창 렌더링, 수동 편집 UI (프론트 책임)
- 세션 트리거·전체 대화 로그(표시용) 보관·컨텍스트 병합·approve된 최종 플로우의 영구 저장 (orchestration_server / 상위 백엔드 책임)
- Unity SDK 데이터 수집 (SDK / orchestration 책임)
- QA 실행 agent, 버그 리포트 agent (별도 흐름 — 이 설계는 시나리오 생성 agent 전용)
- LLM 토큰 스트리밍 — 스킵. 이점이 대화창 타이핑 효과뿐이고, 단일 strict JSON 출력과 충돌하며, 캔버스는 부분 렌더링이 무의미. 결과는 최종 이벤트로 push한다.
- 수동 편집 delta의 명시적 인지/언급 — 불필요(드롭). 매 턴 최신 draft를 권위로 받으므로 편집이 이미 반영됨. 이전 draft 저장/diff 불필요.

## Context / Constraints

- **agent_server는 단일 컨테이너의 단일 서버**로 운용한다. (수평 확장 비대상)
- 세션 상태 저장은 **Redis(key-value)**. 단일 인스턴스지만 **재접속/재시작 내구성**을 위해 in-memory가 아닌 Redis 사용.
- orchestration_server가 세션을 **연다(트리거)**. 모든 Unity SDK 데이터·게임 컨텍스트는 orchestration을 거쳐 **이미 병합된 상태**로 agent_server에 들어온다.
- 화면 표시용 **전체 대화 로그는 상위(orchestration/프론트)**가 보관한다. agent_server는 **프롬프트 재구성에 필요한 최근 N턴만** 보관한다.
- 오픈 시점에 전달되는 Unity context는 **세션 내내 불변**(변경 state가 아님) → 오픈 스냅샷으로 동결. 세션 중 context-update 경로 불필요.
- 기존 코드 자산: LLM 추상화(`app/llm`), strict json_schema / json_object response_format 빌더(`app/llm/json_schema.py`), 모델 레지스트리(`app/llm/models.py`, 기본 `gpt-4o-mini`), `ScenarioAgent`/스키마(`app/agents`), lifespan에 `OpenRouterClient` 배선(`app/main.py`). strict 출력은 이미 적용됨.

## 책임 경계 (Ownership)

| 데이터 / 책임 | 성격 | 소유 |
|---|---|---|
| unity_context + game_context | 세션 정적(불변) | orchestration이 병합 → **오픈 시 1회** agent_server에 전달, agent_server가 Redis 캐시 |
| draft | 사용자 권위(캔버스 편집) | **매 턴 클라이언트가 동봉** → agent_server는 권위로 수신 (Redis에 미저장) |
| 대화 이력 | 서버 누적 | agent_server가 Redis에 **최근 N=10턴** 보관 (프롬프트용) |
| 전체 대화 로그(표시용) | 누적 | **상위(orchestration/프론트)** |
| 세션 트리거 / approve 저장 | 수명·영속 | **orchestration / 상위 백엔드** |

**agent_server = "이력 인지(history-aware) 무상태 한 턴 변환기" + 세션 캐시 소유**
입력 `(session_id, [캐시된 context], history, draft, user_input, model)` → 출력 `(message, scenario)`

## 데이터 수명 분리 (핵심 원칙)

- **정적(context)** → 오픈 시 1회 전달 후 Redis 캐시. 매 턴 재전송하지 않음.
- **사용자 권위(draft)** → 매 턴 동봉. 들어온 draft를 현재 진실로 간주 → 수동 편집이 자동 존중됨. **이전 draft는 저장하지 않음**(correctness에 불필요; 들어온 draft에 편집이 이미 반영됨).
- **서버 누적(history)** → agent_server가 최근 10턴만 Redis에 보관.

## API 표면 (REST + WS)

### 1) `POST /sessions` — 세션 오픈 (생성만, 첫 생성은 WS 연결 시점)

- 요청: `{ unity_context, game_context, user_input, model? }`
- 처리: 유효성 검증 → Redis 키 `session:{id}` 생성 = `{ unity_context, game_context, history: [], pending_user_input: user_input, model }`, TTL 1h → `session_id` 즉시 반환.
- 응답: `{ session_id }` (LLM 생성 없음 → 빠른 응답)
- 근거: 긴 동기 POST(게이트웨이 타임아웃) 회피 + 결과 채널을 WS로 단일화. (4번 결정 = B안: 첫 결과도 WS로 push)

### 2) `WS /sessions/{session_id}` — 대화 채널

- **연결 시**: Redis에서 세션 로드 → `pending_user_input`이 있으면 **첫 턴 실행**(빈 draft + 해당 입력) → 결과 push → `pending_user_input` 소거.
- **클라 → 서버 메시지** (턴): `{ type: "turn", user_input, draft, model? }`
- **서버 → 클라 이벤트**:
  - `{ type: "result", message, scenario }`
  - `{ type: "error", code, detail }` (예: `session_expired`, `llm_error`, `validation_error`)
- 첫 턴이든 이후 턴이든 **모든 결과가 이 채널로** 도착(단일 규약).

### 3) `POST /sessions/{session_id}/approve` / `POST /sessions/{session_id}/decline` — 종료

- 처리: Redis 키 **즉시 DEL**(evict). TTL은 버려진 세션 청소용 안전망일 뿐.
- approve는 최종 플로우가 상위로 인계되는 지점(저장 자체는 상위 책임). agent_server는 세션만 정리.

## Redis 스키마

```
key:   session:{session_id}
value: {
  unity_context: {...},        # 오픈 시 동결
  game_context:  {...},        # 오픈 시 동결
  history: [                   # 최근 10턴만 (append 시 초과분 drop)
    { role: "user",      content: "..." },
    { role: "assistant", content: "..." },   # 채팅 메시지 텍스트만 (draft 미저장)
    ...
  ],
  pending_user_input: "..."|null,  # 첫 턴 트리거용 (소비 후 null)
  model: "openai/gpt-4o-mini"
}
TTL:   3600s (슬라이딩 — 매 턴 갱신)
evict: approve/decline 시 즉시 DEL
```

- **이력 윈도우 N = 10턴**: 저장 자체를 최근 10턴으로 캡(구현 단순). 오래된 턴은 agent_server에선 소실(전체 로그는 상위 보관).
- **history엔 채팅 텍스트만** 저장. 과거 draft 버전은 저장 안 함(현재 draft가 매 턴 들어오므로 불필요).

## 세션 시퀀스

```
[오픈]
orchestration ──POST /sessions {context, user_input}──▶ agent_server
                                     └ Redis 생성(TTL 1h), {session_id} 반환
orchestration ──WS 연결 /sessions/{id}──▶ agent_server
                                     └ pending_user_input 소비 → 첫 턴 실행
                                       (빈 draft + 입력) → LLM → result push
                                       → history append(cap 10), TTL 갱신

[반복 턴]
orchestration ──WS {type:turn, user_input, draft}──▶ agent_server
   1. Redis load {context, history}  (miss → error:session_expired → 상위 재오픈)
   2. 프롬프트 = system + 최근10턴 history + 현재턴(context + 들어온 draft + user_input + output_contract)
   3. model 선택 → response_format(strict json_schema | json_object 폴백) → complete()
   4. parse + Pydantic 검증 → ScenarioAgentResult
   5. WS {type:result, message, scenario} push
   6. history append(user+assistant, cap 10), TTL 갱신

[종료]
orchestration ──POST /sessions/{id}/approve|decline──▶ agent_server
                                     └ Redis 즉시 DEL
```

## 턴 계약 (Request/Response)

- **오픈 입력**: `unity_context`, `game_context`, `user_input`, `model?`
- **턴 입력**: `user_input`, `draft`(현재 캔버스 상태, 권위), `model?`
- **출력**: `{ message, scenario }` — `message`는 대화창, `scenario`(= ScenarioDraft: title/description/steps[]) 는 캔버스. (기존 `ScenarioAgentResult` 구조 그대로)
- **모델 선택**: 프론트가 턴/세션 단위로 선택. `ScenarioAgent`가 현재 생성자 고정이므로, **요청별 생성** 또는 `model`을 `run()` 파라미터로 빼는 리팩터 필요(무상태라 저렴).

## base_agent 관점 (세션 구조화 방향)

- **세션 I/O는 agent가 아니라 세션/WS 핸들러 계층에 둔다.** `ScenarioAgent`는 순수 함수로 유지: `(user_input, context, history, draft) → (message, scenario)`. Redis를 직접 만지지 않음 → 테스트 용이·무상태 유지.
- `AgentContext.session_id`는 **correlation 전용**(로깅/트레이싱/타임아웃 스코프). 상태키 아님.
- **`SessionStore`** (Redis 어댑터, 인프라 계층): `create / load / append_history / refresh_ttl / delete`. WS 핸들러가 사용.
- 기존 `ScenarioAgentRequest`에 **`history` 필드 추가** 필요(핸들러가 로드해 주입). context는 불투명 병합 블롭으로 유지.
- 미래 무상태 agent(QA 실행 등)는 SessionStore를 쓰지 않음 — 세션 인프라를 base에 강제하지 않는다.

## 견고성 / 예외 처리 (agent_server 책임)

- **JSON 파싱 방어 + 재시도**: strict 모델은 형식 강제되나, json_object 폴백/이탈 대비 코드펜스 제거 + 1회 재시도. 실패 시 `error:validation_error` 이벤트.
- **캐시 미스 복구**: 턴 도착 시 세션 키 부재(TTL 만료/재시작/evict 후 지연 도착) → `error:session_expired` → orchestration이 context와 함께 재오픈. (조용히 빈 컨텍스트로 진행 금지)
- **LLM 오류**(402/timeout 등): `error:llm_error`로 표면화. 세션은 유지(재시도 가능), 상위가 대응.
- **동시성**: 단일 인스턴스 + 세션당 WS 1개. **세션당 in-flight 턴 1개로 직렬화**(history read-modify-write 경합 방지). 교차 세션은 서로 다른 key라 무관.

## Integration (orchestration_server 관점)

agent_server가 노출하는 엔드포인트와 orchestration의 구동 순서.

| # | 메서드/경로 | 용도 | 보내는 것 | 받는 것 |
|---|-------------|------|-----------|---------|
| 1 | `POST /sessions` | 세션 오픈 | `{ unity_context, game_context, user_input, model? }` | `{ session_id }` |
| 2 | `WS /sessions/{session_id}` | 대화 채널 | `{ type:"turn", user_input, draft, model? }` | `{ type:"result", message, scenario }` / `{ type:"error", code, detail }` |
| 3 | `POST /sessions/{session_id}/approve` | 승인·종료 | (없음) | `{ ok:true }` |
| 4 | `POST /sessions/{session_id}/decline` | 폐기·종료 | (없음) | `{ ok:true }` |

**구동 순서**
```
[오픈] context 병합 → POST /sessions → {session_id} → WS 연결
       → (연결 시 첫 턴 자동 실행) → {type:result} 수신 → 대화창/캔버스 중계
[반복] WS {type:turn, user_input, draft} → {type:result} 수신 → 중계
[종료] POST /sessions/{id}/approve | decline → 세션 즉시 삭제
```

**orchestration의 책임**
- unity/game context **병합** 후 오픈 시 전달. **세션 동안 context를 자체 보관**(재오픈용).
- 매 턴 **현재 draft(캔버스 최신본) 동봉** — 미동봉 시 편집 미반영.
- context는 **재전송 안 함**(캐시됨). 오픈 응답은 `session_id`만, **첫 초안은 WS로** 도착.
- **턴 직렬화**(이전 result 수신 전 다음 turn 금지). **표시용 전체 대화 로그는 orchestration이 보관**.
- 종료 시 approve/decline **반드시 호출**(미호출 시 1h TTL까지 잔류).

**에러 대응 (`{type:"error", code}`)**

| code | 의미 | 대응 |
|------|------|------|
| `session_expired` | 세션 캐시 만료/부재 | context와 함께 **재오픈** 후 재연결 |
| `llm_error` | LLM 호출 실패 | 세션 유지 → 재시도 유도 |
| `validation_error` | 출력 검증 실패(재시도 후) | 재시도 또는 사용자 안내 |

## Approach (Checklist)

- [x] **Step 0: Recon** — 현재 `app/agents`, `app/llm`, `app/main.py`, `app/api` 재확인, Redis 클라이언트(`redis.asyncio`) 의존성 검토
- [x] **Step 1: Implementation**
  - [x] `SessionStore`(Protocol + InMemory + `RedisSessionStore`) + 설정(`redis_url`, `session_ttl_seconds=3600`, `history_max_turns=10`)
  - [x] `ScenarioAgentRequest`에 `history`/`unity_context`/`game_context`/`model` 추가, `ScenarioPromptBuilder`가 history를 메시지로 펼치고 수동편집 존중 문구 강화, `ScenarioContext`(느슨한 타입) 제거
  - [x] `ScenarioAgent` 모델을 요청별(`request.model`) 선택
  - [x] `POST /sessions`, `WS /sessions/{id}`, `POST .../approve|decline` 라우트 + `SessionService`
  - [x] JSON 파싱 방어(`json_parse.extract_json_object`) + 1회 재시도, 도메인 예외 → WS error(`session_expired`/`validation_error`/`llm_error`) 매핑
  - [x] lifespan에 Redis 연결/해제 배선
- [x] **Step 2: Tests** — `SessionService`/`InMemorySessionStore` 단위테스트, 세션 시퀀스(오픈→첫턴→턴→종료) WS 테스트(FakeLLMClient), 만료·이력캡·재시도 경로 테스트
- [ ] **Step 3: Rollout / Rollback** — Redis 컨테이너 추가(compose) **미완**, 롤백은 세션 계층/라우트 제거

## Validation

- **Commands to run:** `python -m pytest`
- **Expected output:** 전 테스트 통과. (LLM 실호출 없이 FakeLLMClient + InMemorySessionStore로 검증 — 실제 LLM 호출·과금 없음)
- **Result:** 오프라인 19개 통과 확인. 실 LLM 연동은 크레딧 충전 후 별도 검증 필요.

## Risks & Rollback

- **Risks:**
  - context를 1회만 전달 → 캐시 미스 시 세션 지속 불가. 슬라이딩 TTL + 명시적 만료 에러 + 상위 재오픈으로 완화.
  - 단일 인스턴스 전제 — 향후 확장 필요 시 세션 스토어는 Redis라 이전 가능하나, WS 연결 어피니티/직렬화 재설계 필요.
  - `history`를 최근 10턴으로 캡 → agent_server에선 오래된 맥락 소실(전체 로그는 상위 보관 전제).
  - strict json_schema 미지원 폴백 모델 사용 시 형식 이탈 가능 → 파싱 방어·재시도로 완화.
- **Rollback steps:** 라우트·SessionStore·Redis 배선 revert, `ScenarioAgentRequest.history` 및 프롬프트 변경 revert.

## Open Questions

- 라우트 세부: approve/decline을 REST로 둘지, WS 종료 메시지로도 받을지 (현재 **REST**로 구현).
- `model` 선택 단위: 세션 고정 vs 턴별 변경 (현재 **오픈 시 세션 기본 + 턴 payload로 override 가능**).
- history 캡 (현재 **저장 자체를 10턴=20메시지로 캡** — 단순).
- 첫 턴 실패(WS 연결 직후 LLM 오류) 시 세션 유지/폐기 정책 — **미확정**(현재 구현은 세션 유지, 에러 이벤트 후 연결 유지).
- Redis 컨테이너 배포(compose/Jenkins) 구성 — 후속 작업(Step 3).
