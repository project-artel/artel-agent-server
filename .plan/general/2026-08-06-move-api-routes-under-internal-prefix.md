# 2026-08-06 — agent-server API 경로를 /internal/** 로 통일

- Date: 2026-08-06
- Jira: ARTEL-270
- Status: Implemented (PR 대기)

## Goal

업무 엔드포인트 10개를 전부 `/internal` 접두사 아래로 옮긴다. 접두사가 신뢰
경계를 표현하게 만들어, 나중에 공개 API가 추가될 때 옮길 것이 남지 않게 한다.

- 10개 전부 `/internal` 아래에서 응답 (WebSocket 2개 포함)
- `/health`, `/docs`, `/redoc`, `/openapi.json`은 접두사 없이 유지
- 접두사 없는 옛 업무 경로 10개는 404
- orchestration 코드 변경 0 (환경변수만으로 전환)
- `.agents/docs/project.md`의 "API 표면과 신뢰 경계" 절이 구현과 일치

## Non-goals

- 인증 추가(공유 시크릿·mTLS). `app-net` 신뢰 전제 유지
- 포트 분리. agent-server에는 공개 호스트가 없다
- 호환 alias·이중 마운트·후속 제거 티켓 — 명시적으로 거부
- 요청·응답 계약 변경. 경로만 바뀐다
- 공개 API 설계

## Context / Constraints

**Hard cutover.** 옛 경로를 남기지 않으므로 배포 창에서 QA·시나리오가 멈춘다.
창의 길이는 agent-server 배포 완료부터 orchestration 재배포 완료까지다. 이
비용이 받아들일 만한 이유: agent-server 배포는 `docker stop` + `docker run`이라
진행 중인 QA 런과 WS 세션이 어차피 전부 끊긴다. 이중 마운트가 지불하는 비용은
영구적이고, 그러면 "접두사가 신뢰 경계다"라는 결론 자체가 무효가 된다.

**배포 선행 조건 (레포 밖).** orchestration `.env`(Jenkins Credentials Secret
file)의 `ARTEL_AGENT_BASE_URL`·`ARTEL_AGENT_WS_BASE_URL`에 `/internal`을 붙여
**agent-server 배포 전에 미리** 갱신 업로드해 둔다. .env를 나중에 준비하면 창이
사람 손 속도만큼 길어진다.

**orchestration 호출부 조립 방식.** 대부분 문자열 연결(`"$agentBaseUrl/embed"`,
`"$baseUrl/qa-sessions"`, `"$agentWsBaseUrl/sessions/$id"`)이라 base URL만
바꾸면 따라온다. `QaModelCatalogService`만 WebClient `baseUrl` + `.uri("/models")`
방식이다. 읽기 전용으로 실제 확인함:
`src/main/kotlin/kr/artel/orchestration/qa/service/QaModelCatalogService.kt:23`의
`WebClient.create(baseUrl)`(baseUrl = `artel.agent.base-url`)와 같은 파일 `:40`의
`.uri("/models")`. Spring `DefaultUriBuilderFactory`는 base의 path에 이어 붙이므로
`http://…/internal` + `/models` = `/internal/models`가 되어 정상 동작한다.

**단, base URL 끝에 슬래시가 있으면 안 된다.** `…/internal/`로 업로드하면
path-append가 `/internal//models`를, 문자열 연결 호출부는 `/internal//embed`를
만든다. FastAPI는 이중 슬래시 경로를 리다이렉트하지 않고 404를 낸다. 이것이
`.env` 갱신에서 실수하기 가장 쉬운 지점이며 두 변수 모두에 해당한다.

**착수 시 표 재검증 결과 (완료).** `grep '@router\.'` 로 전수 확인. 티켓의 표
10개와 코드가 정확히 일치하며, 표에 없는 추가 라우트는 없다. 줄 번호도 일치.
`/health`가 유일한 무접두사 업무 외 라우트다. ARTEL-267의 `/internal/llm-usage`는
agent-server가 **보내는** 쪽이라 이 레포에 라우트가 없다 — 무관 확인.

옮기는 10개 (테스트가 대조할 정본. 옛 경로 404 목록도 같은 10개다):

| # | 메서드 | 옛 경로 | 새 경로 |
|---|---|---|---|
| 1 | POST | `/sessions` | `/internal/sessions` |
| 2 | POST | `/sessions/{session_id}/approve` | `/internal/sessions/{session_id}/approve` |
| 3 | POST | `/sessions/{session_id}/decline` | `/internal/sessions/{session_id}/decline` |
| 4 | WS | `/sessions/{session_id}` | `/internal/sessions/{session_id}` |
| 5 | POST | `/qa-sessions` | `/internal/qa-sessions` |
| 6 | WS | `/qa-sessions/{session_id}` | `/internal/qa-sessions/{session_id}` |
| 7 | POST | `/extract` | `/internal/extract` |
| 8 | POST | `/embed` | `/internal/embed` |
| 9 | POST | `/knowledge-queries` | `/internal/knowledge-queries` |
| 10 | GET | `/models` | `/internal/models` |

무접두사 유지: `GET /health`, `/docs`, `/redoc`, `/openapi.json`.

**FastAPI 동작.** `include_router(prefix="/internal")`은 WebSocket 라우트에도
적용된다. 접두사는 라우터 마운트 시점에 붙으므로 각 라우터 모듈의 데코레이터
경로는 손대지 않는다.

## Approach (Checklist)

- [x] **Step 0: Recon**
  - [x] `app/main.py`의 `include_router` 7줄 확인 (104–110행)
  - [x] `grep '@router\.'` 로 라우트 전수 재검증 — 표와 일치, 누락 없음
  - [x] 경로 문자열을 쓰는 테스트 파일 식별: `test_sessions.py`,
        `test_api_sessions_dispatch.py`, `test_api_knowledge.py`,
        `test_extract.py`, `test_qa_run_config_contract.py`,
        `test_qa_model_reasoning.py`. **이 목록은 갱신 대상이 아니다** — 대부분은
        자기 앱에 접두사 없이 라우터를 마운트하므로 그대로 둔다. 실제 갱신
        대상은 Step 2의 UPDATE 목록을 따른다
  - [x] `docs/api-documentation.md`, `README.md`, `Jenkinsfile`, `Dockerfile`에
        업무 경로 하드코딩 없음 확인 (`/docs`, `/redoc`, `/openapi.json`만 등장)

- [x] **Step 1: Implementation**
  - [x] `app/main.py` 모듈 최상단(`create_app` 앞)에 `INTERNAL_PREFIX = "/internal"`
        을 둔다. 새 모듈·`config.py`·환경변수로 빼지 않는다 — 운영자가 바꿀 값이
        아니라 아키텍처 사실이고, 쓰는 곳은 이 파일 하나다
  - [x] `app/main.py`: 업무 라우터 6개(`sessions`, `qa_sessions`, `extract`,
        `embeddings`, `knowledge_queries`, `models`)에 `prefix=INTERNAL_PREFIX`
        추가. `api_router`(`/health`)는 접두사 없이 그대로
  - [x] 각 라우터 모듈의 `@router.*` 경로는 변경하지 않는다 — 접두사는 마운트
        시점 책임

- [x] **Step 2: Tests**

  **기존 테스트 갱신은 기계적 치환이 아니다.** 대부분의 테스트 파일은 자기
  `FastAPI()`를 만들어 `include_router(router)`로 **접두사 없이** 라우터를
  마운트한다. `app/main.py`는 그 앱들에 관여하지 않으므로 거기서 경로를 바꾸면
  전부 404가 된다. `grep -rn "app.main" tests/`로 확인한 결과 실제 앱을
  쓰는 파일은 4개뿐이다.

  - [x] 갱신 대상 (실제 `app.main.app`을 통과 — 여기만 새 접두사로 바꾼다):
        - `tests/test_qa_run_config_contract.py:37,118,126` — `/qa-sessions`
        - `tests/test_qa_model_reasoning.py:32` — `/models`
        - `tests/test_api_knowledge.py:163-171` — 이 파일에서
          `test_both_endpoints_are_published_in_the_contract` **하나만**.
          스키마 키가 `/internal/embed`, `/internal/knowledge-queries`가 된다
  - [x] 손대지 않는 것 (로컬에 접두사 없이 마운트한 앱을 쓴다):
        - `tests/test_sessions.py` — `_test_app()`(240-246) 및 인라인 앱
          (273-288, 365-371). 모든 `/sessions` 경로와 WS 연결 그대로
        - `tests/test_api_sessions_dispatch.py` — `_app()`(41-42)
        - `tests/test_api_knowledge.py` — `_embed_client`/`_queries_client`
          (37-54)와 그것들을 거치는 모든 요청
        - `tests/test_extract.py` — `_app_with_service()`(88-90)
        - `tests/test_api.py` — `/health`·`/openapi.json`만 본다

  - [x] 신규 표면 테스트 (OpenAPI 스키마 기반). 스키마를 읽으면 핸들러 의존성
        (Redis·임베딩 클라이언트·추출 서비스)을 다 세우지 않고도 마운트 결과를
        직접 대조할 수 있고, 이 레포에 이미 있는 방식이다(`test_api.py:8`의
        `TestClient(app)` — lifespan 없이 구성). 확인 항목:
        - 표의 HTTP 8개가 전부 새 경로로 스키마에 존재하고 메서드가 맞다
        - 옛 무접두사 업무 경로 8개가 스키마에 **없다**
        - 스키마의 무접두사 경로 집합이 정확히 `{"/health"}`다. `/docs`,
          `/redoc`, `/openapi.json`은 애초에 `paths`에 나타나지 않는다. 집합을
          통째로 대조하면 앞으로 접두사 없이 추가되는 **HTTP** 라우트를 이
          테스트가 잡는다 (WS는 아래 참고)
  - [x] 신규 런타임 404 테스트: 옛 HTTP 경로 8개에 실제 요청을 보내 404 확인.
        스키마에 없는 것과 실제로 404인 것은 다르다

  - [x] 신규 WS 테스트 (WS는 OpenAPI 스키마에 없으므로 별도). **두 WS 핸들러는
        `accept()` 이전에 `app.state`에서 서비스를 꺼낸다**
        (`app/api/sessions.py:126`, `app/api/qa_sessions.py:108`). lifespan은
        살아 있는 Redis를 요구하므로 돌리면 안 되고, 그냥 연결하면
        `AttributeError: 'State' object has no attribute 'session_service'`가
        난다. 따라서:
        - `TestClient(app)`을 `with` 없이 쓴다 (lifespan 미실행)
        - `test_qa_run_config_contract.py:20-30`의 autouse 픽스처와 같은 방식으로
          인메모리 서비스를 `app.state`에 직접 설치한다:
          `SessionService(store=InMemorySessionStore())`,
          `QaExecutionService(store=InMemoryQaSessionStore())`
        - 연결이 수락된 근거로 없는 id에 대한 첫 프레임을 읽는다:
          `/internal/sessions/unknown` → `{"type": "error", "code":
          "session_expired", ...}`,
          `/internal/qa-sessions/unknown` → 봉투의 `type == "ERROR"`,
          `payload.code == "session_expired"`
  - [x] 신규 옛 WS 경로 테스트: Starlette는 라우팅되지 않은 websocket scope에
        `WebSocketClose()`(code 1000)를 보내고 TestClient가 이를
        `WebSocketDisconnect`로 바꾼다. HTTP 404도 `WebSocketDenialResponse`도
        아니다. 따라서:
        ```python
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/sessions/x"):
                pass
        ```

  - [x] 신규 테스트는 `INTERNAL_PREFIX`를 import하지 않고 `/internal` 문자열을
        직접 쓴다. 상수를 import하면 상수와 테스트가 같이 움직여 아무것도
        검증하지 않게 된다
  - [x] `/health`가 무접두사로 응답하는 것을 명시 확인 (기존 `test_api.py`가
        이미 커버하지만 이 티켓의 AC라 표면 테스트에서도 대조한다)

  **커버리지 정직하게 적기.** AC의 "10개가 응답"은 스키마 존재로 8개,
  라이브 요청으로 `/internal/qa-sessions`·`/internal/models` 2개, WS 2개는
  실제 연결로 확인된다. 스키마 존재는 마운트의 직접 증거이며 이 트레이드오프는
  핸들러 의존성을 전부 세우는 비용을 피하기 위한 것이다.

  **남는 구멍 (pair-review에서 뮤테이션 테스트로 확인).** 표면 테스트는 라우터
  미마운트·무접두사 마운트·이중 마운트를 전부 잡지만, **새로 추가되는 WS 라우트가
  접두사 밖에 붙는 경우는 잡지 못한다.** WS는 스키마에 없고, FastAPI는 접두사가
  적용된 뒤의 WS 실효 경로를 공개 API로 돌려주지 않는다
  (`_EffectiveRouteContext.path`가 `APIWebSocketRoute`에 대해 빈 문자열).
  라우터 내부를 파고들면 막을 수 있지만 FastAPI 업그레이드마다 깨지는 결합을
  사는 것이라 열어 둔다. 대신 테스트 모듈 docstring에 명시하고,
  `project.md`의 접두사 규칙이 그 자리를 맡는다.

- [x] **Step 3: Docs & Rollout**
  - [x] `.agents/docs/project.md`의 "API 표면과 신뢰 경계" 절을 커밋에 포함.
        develop 체크아웃에 미커밋 상태로 있던 초안을 워크트리로 가져왔고,
        구현과 대조해 어긋나면 고친다. 원본 develop 체크아웃 파일은 손대지 않는다
  - [x] PR 본문에 hard cutover 배포 창과 `.env` 사전 갱신 선행 조건 명시
  - [x] PR 본문에 `QaModelCatalogService.kt:23,40` 확인 지점과 base URL 후행
        슬래시 금지를 명시

## Validation

- **Commands to run:**
  - `python -m pytest` (전체 스위트)
- **Expected output:**
  - 전부 통과. 신규 테스트가 확인하는 것: HTTP 8개가 새 경로로 스키마에 존재
    (그중 `/internal/qa-sessions`·`/internal/models`는 라이브 요청으로도),
    WS 2개가 새 경로에서 실제 연결, 옛 HTTP 경로 8개가 런타임 404, 옛 WS 경로
    2개가 `WebSocketDisconnect`, 무접두사 스키마 경로가 `/health` 하나뿐
- **배포 전 선행 조건 (레포 밖, 이 PR로 검증 불가):**
  - orchestration `.env`(Jenkins Credentials Secret file)의
    `ARTEL_AGENT_BASE_URL`·`ARTEL_AGENT_WS_BASE_URL`에 `/internal`을 붙여
    **agent-server 배포 전에** 재업로드했는지 확인. 두 값 모두 후행 슬래시 없이
  - `ARTEL_AGENT_BASE_URL`에 `/internal`을 붙인 orchestration을 로컬로 띄우고
    QA 런 1회 + 시나리오 저작 1회. 모델 카탈로그 조회(`/models`)를 특히 확인
  - `/health`가 접두사 없이 응답 (컨테이너 헬스체크 의존)
- **배포 후:**
  - QA 런 1회를 끝까지 통과시켜 창이 닫혔는지 확인

## Risks & Rollback

- **Risks:**
  - **배포 창.** agent-server 배포 완료 ~ orchestration 재배포 완료 사이 QA·
    시나리오 전면 중단. 완화: `.env` 사전 갱신 후 두 배포를 연달아 실행
  - **`QaModelCatalogService` 조립 경로.** 여기만 어긋나면 모델 카탈로그가 404.
    완화: 배포 전 로컬 확인. 옛 경로가 없으므로 조용한 실패가 아니라 즉시 404로
    드러난다 — hard cutover의 이점
  - **다른 hard cutover와 겹침.** ARTEL-265/267 배포와 겹치면 무엇이 깨졌는지
    가려내기 어렵다. 완화: 265/267 정리 후 배포
  - **레포 밖 소비자.** Insomnia 컬렉션(`project-artel/insomnia-api`의
    `agent-server.yaml`)이 옛 경로를 담고 있다. 이 레포 밖이며 별도 갱신 대상
- **Rollback steps:**
  - `git revert` 후 재배포. orchestration `.env`도 `/internal` 제거본으로 되돌려
    재업로드 + 재배포. 롤백도 hard cutover라 같은 크기의 창을 요구한다

## Rejected feedback

- **WS 테스트에 메시지 송수신·잘못된 session_id 동작까지 넣자.** 거부. 이 티켓은
  경로만 바꾸며 요청·응답 계약은 Non-goal이다. WS 대화 동작은 `test_sessions.py`
  등이 이미 커버하며, 그 파일들은 로컬 마운트 앱을 쓰므로 이 변경과 무관하게
  계속 통과한다. 신규 WS 테스트는 실제 앱에서의 마운트만 확인한다 — 그것이 실제
  접두사에서 WS를 확인하는 유일한 커버리지다.
- **테스트 파일별 갱신 매핑은 생략하자(기계적 치환).** 거부 철회 — heavy 리뷰가
  이 판단을 뒤집었다. 치환은 기계적이지 않다. 대부분의 테스트가 자기 앱에
  접두사 없이 라우터를 마운트하므로 거기서 경로를 바꾸면 404가 된다. Step 2에
  갱신 대상과 비대상을 파일·줄 단위로 명시했다.

## Open Questions

- 없음. 계약이 티켓에 전부 적혀 있고 코드 재검증에서 차이가 나오지 않았다.
