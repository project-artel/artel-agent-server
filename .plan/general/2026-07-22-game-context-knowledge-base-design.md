# 2026-07-22 — 게임 정보 KB(문서 → game_context) 설계

- Date: 2026-07-22
- Jira: TBD (미생성 — 사용자 승인 후 `## Jira-Driven Development Flow`로 생성)
- Status: Draft

## Goal

기획서 등 게임 관련 문서를 **프로젝트 단위로 업로드·등록**하면, 에이전트가 문서에서
게임 사실을 추출해 구조화된 `game_context`로 만들어 **누적**하고, QA 시나리오 세션이
이 정보를 **공유**해서 쓰게 한다.

핵심 성질 세 가지:
- **프로젝트 지식 베이스(KB)**: 원하는 시점에 파일을 여러 개 올려 정보를 쌓는다(1회성 아님).
- **동일 파일 재사용**: 같은 내용의 파일을 다시 올리면 재추출하지 않고 등록된 결과를 재사용(dedup).
- **세션 공유**: QA 세션은 프로젝트의 집계된 `game_context`를 오픈 시점 스냅샷으로 받아 쓴다.

## Non-goals

- 프론트 업로드 UI, 문서 뷰어/버전 관리 화면 (프론트/상위 책임)
- 시나리오 세션 메커니즘(REST+WS, 반복 개선, approve/decline) — **이미 구현됨**. 본 설계는
  세션이 소비하는 `game_context`의 **출처(source)**를 새로 만들 뿐, 세션 계약은 건드리지 않는다.
- QA 실행 agent / 버그 리포트 agent
- 멀티테넌트 인증·권한, 원본 파일의 영구 보관/다운로드(상위/오브젝트 스토리지 책임 — KB는 추출 결과만 보관)
- 문서 간 모순을 LLM으로 화해(reconcile)시키는 고급 병합 — v1은 결정적 union (아래 병합 전략)

## Context / Constraints

- 기존 아키텍처: agent_server는 **단일 컨테이너 단일 서버**. 에이전트는 순수 함수, 세션 I/O는
  `SessionStore`(Redis 어댑터)로 분리(`app/sessions`). LLM은 모델 슬러그별 지연 생성(`app/llm`).
- 현재 `game_context`는 **불투명 dict** — `POST /sessions`로 받아 오픈 시 동결, 프롬프트에
  `json.dumps`로 통째 주입될 뿐 스키마·검증 없음 (`app/agents/scenario_prompt.py:62`). 소비자는
  LLM 하나 → 구조의 목적은 다운스트림 파싱이 아니라 **LLM 가독성 + 추출 일관성**이다.
- 시나리오 step은 `state / action / expected` 3요소(`scenario_prompt.py`). 따라서 KB 스키마는
  이 3요소에 매핑되는 정보(화면=state / 조작=action / 규칙=expected)를 **우선** 담는다.
- 기획서 관찰(wordventure): 정제된 스펙이 아니라 **팀 작업 노트**. 신호(장르/코어루프/시스템/
  스테이지/튜토리얼 스크립트)와 노이즈(개발 일정, 담당자 배정, 에셋스토어 링크, 회식)가 섞여 있다.
  게임마다 문서 형식이 완전히 다르므로 **고정 템플릿 파싱 불가** → LLM이 잡음을 걸러 의미 단위로 재구성.
- **신규 영속 데이터 도입**: `project.md`상 "Persistent data: None yet". KB는 이 저장소 최초의
  durable 데이터다. Redis는 TTL/휘발이라 KB 백엔드로는 부적절 → 백엔드 선택은 Open Question.

## 파일 → 텍스트 추출 배치 결정 (why ②)

| 배치 | 요지 |
|---|---|
| ① 상위가 추출, agent는 텍스트만 | 의존성 0·테스트 쉬움 but 업로드 UX가 두 서버로 쪼개짐, 구조 소실 |
| **② agent_server 내부 `document_loader` 모듈이 추출** ⭐ | KB가 한 서버에 응집·단일 배포, 추출 튜닝 가능. 로더를 분리하면 **에이전트는 텍스트만** 받아 ①의 테스트 이점도 확보. 대가: pypdf/pdfplumber 의존성 |
| ③ 멀티모달 LLM에 페이지 이미지 투입 | 다이어그램·표 최고 충실도 but 비용/지연·토큰 한계 |

**결정: v1 = ②.** `document_loader`를 **전략 인터페이스**로 두어 기본은 텍스트 추출,
추후 ③(멀티모달)을 로더 전략으로 끼워넣을 수 있게 한다. 에이전트 계약은 "텍스트 → 구조화
game_context"로 고정 → 되돌리기 쉬움.

**PDF 추출기 = pdfplumber (pypdf 대신).** 동일 PDF 비교에서 pypdf는 단어별 파편화·음절 사이 공백이
심했고, pdfplumber는 문장·줄·목록 구조를 보존했다(특히 튜토리얼 스크립트의 step/트리거 라인 →
`flows[].steps` 추출 품질에 직결). pdfplumber도 순수 파이썬(pdfminer.six) 단일 의존성이라
네이티브 바이너리 불필요.

## 책임 경계 (Ownership) — 중심 결정

KB의 **durable 저장·프로젝트 관리**를 agent_server가 갖느냐 상위(orchestration)가 갖느냐가
가장 큰 갈림길이다(기존 설계는 context 병합·영속을 상위로 밀었음). 두 안:

| | A. agent_server가 KB 소유 (추천) | B. 상위가 저장, agent_server는 추출만 |
|---|---|---|
| 추출 에이전트 | agent_server | agent_server |
| dedup(해시)·문서 레지스트리·집계 | **agent_server** (`DocumentStore` Protocol + 어댑터, `SessionStore` 패턴 복제) | 상위 |
| durable 백엔드 | agent_server가 소유(Postgres/오브젝트 스토어 등) | 상위 DB |
| 세션 연동 | 상위가 `GET /projects/{id}/game-context`로 집계본 조회 → `POST /sessions`에 인라인 전달 | 상위가 자체 저장분을 인라인 전달 |
| 장점 | KB 기능 응집, UX 단일, 재사용 로직을 에이전트 옆에 | agent_server 무상태 유지, 영속은 상위 일원화 |
| 단점 | agent_server가 첫 영속 데이터 소유 | 추출↔저장이 분리돼 dedup 왕복↑, 스키마 계약 이중화 |

**추천: A.** dedup·집계는 추출 결과 구조를 잘 아는 쪽(에이전트 옆)에 두는 게 응집적이고,
`SessionStore`라는 저장 추상화 선례가 이미 있다. 단, **원본 파일 바이트의 영구 보관은 KB 책임 아님** —
KB는 `(project_id, content_hash) → 추출 결과`만 보관한다. (최종 확정은 Open Questions에서 상위와 합의.)

## 데이터 모델

```
Project            : 게임 하나. project_id (외부에서 부여/전달).
SourceDocument     : 업로드된 파일 1개.
  - document_id
  - project_id
  - filename
  - content_hash   (sha256(파일 바이트)) — dedup 키
  - loader_kind    ("pdf" | "text" | ...)
  - created_at
  - extraction     : 이 문서에서 추출한 GameContext 조각 (아래 스키마)
AggregatedGameContext : 프로젝트의 모든 활성 SourceDocument.extraction 병합 결과.
                        = 세션이 받는 game_context.
```

- **dedup 범위 = 프로젝트별** (키 `(project_id, content_hash)`). 전역 재사용은 한 게임 정보가
  다른 게임으로 새는 위험 → 금지.
- 파일 수정 시 바이트가 달라져 새 해시 → 새 문서로 재추출(기존은 명시적 삭제로 교체).

## game_context 스키마 (범용 골격)

장르 불문 재사용되도록 **소수 상위 섹션 + 각 섹션은 자유형 엔트리 리스트**. 모든 섹션 optional·가변 길이.
각 엔트리는 추적을 위해 `source`(document_id)를 달 수 있다.

**핵심 규칙 — 고정 틀 / 자유형 내부:**
- **top-level 섹션 = 고정·소수·안정적**. 게임마다 새 top-level 키가 생기지 **않는다**.
- **게임별 다양성은 내부(엔트리와 그 자유형 필드)로 흡수**한다. 예: wordventure의 상태이상·속성
  상성·카드 조합은 새 섹션이 아니라 `mechanics`의 엔트리로, 적/아이템 특수 능력치는
  `entities[].properties`(자유형 dict)로 들어간다. → 다양성은 **깊이**로, 너비(top-level)는 고정.
- 이유: 추출/시나리오 에이전트의 예측 가능성 + 문서 간 병합(같은 섹션 위에서 union)을 지키기 위함.

```json
{
  "overview":    { "title": "...", "genre": "...", "platform": "...",
                   "summary": "...", "core_loop": "..." },
  "screens":     [ { "name": "조합창", "purpose": "...",
                     "elements": ["마법 칸","속성 칸","조합 버튼"],
                     "transitions": ["배틀씬에서 조합 버튼 클릭 시 진입"],
                     "source": "doc_..." } ],
  "mechanics":   [ { "name": "키워드 조합", "description": "...",
                     "rules": ["주문 3종 × 속성 5종 = 15마법"],
                     "preconditions": ["초반엔 Fire 속성만 사용 가능"],
                     "source": "doc_..." } ],
  "entities":    [ { "name": "슬라임", "type": "enemy", "properties": {}, "source": "doc_..." } ],
  "progression": [ { "name": "평원", "order": 1, "notes": "...", "source": "doc_..." } ],
  "flows":       [ { "name": "튜토리얼", "steps": ["카드 뽑기","조합창 열기","Fire 넣고 조합"],
                     "source": "doc_..." } ],
  "glossary":    [ { "term": "속성", "meaning": "불/물/...", "source": "doc_..." } ],
  "misc":        [ { "note": "7개 섹션 어디에도 안 맞는 드문 정보", "source": "doc_..." } ]
}
```

- `screens` / `mechanics` / `flows`가 핵심 — 시나리오 step의 state/action/expected에 직결.
- `flows`는 문서에 이미 있는 시퀀스(튜토리얼 스크립트 등)를 담으면 시나리오 에이전트가 바로 활용.
- 스키마는 **느슨한 Pydantic(`extra="allow"`)**으로 검증 — 게임별 편차 흡수(예외 필드 삼켜도 깨지지 않음).
- **안전판 `misc`**: 7개 섹션 어디에도 안 맞는 드문 정보만. 프롬프트에서 "가능하면 기존 섹션에
  넣고, 정말 안 맞을 때만 `misc`"로 유도해 남용을 막는다.

## 병합(집계) 전략 — v1

- 리스트 섹션(`screens`/`mechanics`/`entities`/`progression`/`flows`/`glossary`):
  **모든 문서의 엔트리를 union + 경량 dedup**(예: `name`/`term` 정규화 후 중복 제거),
  각 엔트리에 `source` 유지.
- `overview`(단일 객체): 문서 순서대로 **빈 필드 채우기(fill-gaps)**, 이미 채워진 필드는 유지.
  (충돌 시 최초 우선; 다중 overview가 흔하면 후속 개선.)
- **LLM 화해 패스 없음**(결정적·저비용·예측 가능). 필요 시 후속 작업으로 추가.
- 집계는 조회 시 계산하거나 캐시 후 ingest/삭제 시 무효화 — 둘 다 가능, 구현 단순 쪽 선택.

## API 표면 (제안, A안 기준)

세션 계약은 그대로 두고 KB API를 **별도**로 추가한다.

| # | 메서드/경로 | 용도 | 보내는 것 | 받는 것 |
|---|---|---|---|---|
| 1 | `POST /projects/{project_id}/documents` | 문서 등록 | **presigned S3 GET URL**(+선택 `content_hash`) 또는 파일(멀티파트/`content_b64`) | `{document_id, content_hash, reused, extraction_summary}` |
| 2 | `GET /projects/{project_id}/documents` | 등록 문서 목록 | (없음) | `[{document_id, filename, content_hash, created_at}]` |
| 3 | `DELETE /projects/{project_id}/documents/{document_id}` | 등록 해제 | (없음) | `{ok: true}` |
| 4 | `GET /projects/{project_id}/game-context` | 집계 game_context 조회 | (없음) | `{ game_context: {...} }` |

- **ingest 흐름**: (presigned URL이면) S3에서 바이트 fetch → **바이트 기준 `sha256`으로 `content_hash`**
  (URL이 아님 — presigned URL은 만료·가변, 같은 key 덮어쓰기 가능) → `(project_id, hash)` 존재하면
  **재사용**(추출 skip, `reused=true`) → 없으면 `document_loader`로 텍스트화 → `GameContextAgent` 추출 → 저장.
- **S3 참조 주의**: presigned GET URL 권장(agent_server에 AWS 자격증명 불필요, HTTP GET만).
  임의 URL fetch SSRF 방지(버킷 호스트 화이트리스트), 크기 상한·만료 처리. 원본 파일 영구 보관은 상위 소유.
- **세션 연동**(무변경): 상위가 오픈 전에 `GET /game-context`로 집계본을 받아 기존
  `POST /sessions`의 `game_context`에 그대로 인라인 전달. 기존 세션 설계 유지.

## 컴포넌트 (기존 패턴 매핑)

- `app/agents/game_context_schemas.py` — `GameContext`, 섹션 스키마 (`scenario_schemas.py` 패턴)
- `app/agents/game_context_prompt.py` — 추출 프롬프트: **"일정·담당자·에셋링크 등 노이즈 제외,
  게임 동작/규칙/화면 사실만"** 명시 (`scenario_prompt.py` 패턴)
- `app/agents/game_context_agent.py` — `GameContextAgent`: 텍스트/블록 → `GameContext`
  (`ScenarioAgent` + `with_structured_output` + 재시도 패턴 복제)
- `app/documents/loader.py` — `DocumentLoader` 전략(Protocol): 파일 바이트 → 정규화 텍스트.
  `pdf`(pdfplumber), `text/md`(decode passthrough) 구현. 파일 fetch(S3 등)는 서비스 책임,
  loader는 순수 `bytes → str`. ③ 멀티모달은 후속 전략. **[구현 완료]**
- `app/documents/store.py` — `DocumentStore`(Protocol) + `InMemoryDocumentStore` + durable 어댑터.
  키 `(project_id, content_hash)` (`SessionStore` 패턴 복제).
- `app/documents/service.py` — ingest(dedup)·목록·삭제·집계(병합) 오케스트레이션.
- `app/api/documents.py` — 위 라우트. `app/main.py` lifespan에 store/service 배선.
- `app/config.py` — 백엔드 설정(예: `document_store_url`), 지원 포맷·최대 파일 크기.

## Approach (Checklist)

- [ ] **Step 0: Recon / 결정 확정** — Ownership(A/B), durable 백엔드, 파일 크기·포맷 한계, dedup 범위,
      세션 연동 방식(인라인 전달)을 Open Questions에서 확정.
- [x] **Step 1: 스키마·에이전트** — `GameContext` 스키마 + `GameContextAgent` + 프롬프트, FakeLLM 단위테스트.
- [x] **Step 2: 로더** — `DocumentLoader`(pdfplumber/text) + 텍스트 픽스처·monkeypatch 테스트. 실 PDF 스모크 확인.
- [ ] **Step 3: 저장·서비스** — `DocumentStore`(InMemory+durable) + ingest(dedup)·집계 로직 + 테스트
      (같은 해시→reused, 다중 문서→union 병합).
- [ ] **Step 4: API·배선** — 라우트 4종 + lifespan 배선 + API happy-path 테스트.
- [ ] **Step 5: 롤아웃** — durable 백엔드 컨테이너/설정(compose), 상위(orchestration) 연동 문서.

## Validation

- **Commands:** `python -m pytest`
- **Expected:** 신규 테스트 포함 전 통과. LLM 실호출 없이 FakeLLM + InMemory 스토어로 검증(과금 없음).
- **품질 확인(별도):** wordventure 기획서로 실제 추출을 1회 돌려 노이즈 필터링·섹션 채움을 육안 확인
  (크레딧 필요 → 별도 검증).

## Risks & Rollback

- **Risks**
  - 첫 durable 데이터 도입 → 백엔드·마이그레이션·백업 정책 필요(신규 운영 부담).
  - 추출 품질 편차: 문서가 지저분하거나(단어별 줄바꿈) 다이어그램 중심이면 텍스트 추출 손실 →
    ③ 멀티모달 로더로 완화 가능(전략 교체).
  - 병합 union의 중복/모순 → v1은 source 태깅·경량 dedup으로 완화, LLM 화해는 후속.
  - Ownership A 채택 시 기존 "context 영속은 상위" 원칙과 부분 상충 → 상위와 경계 합의 필요.
- **Rollback:** KB 라우트·`app/documents`·에이전트·배선 revert. 세션 계약은 무변경이라
  KB 제거해도 기존 세션 흐름은 그대로 동작(상위가 game_context 인라인 전달로 복귀).

## Open Questions

- **Ownership**: KB durable 저장을 agent_server(A) vs 상위(B) 어디에? (추천 A) — 상위 팀과 합의 필요.
- **Durable 백엔드**: Postgres(JSONB) / 오브젝트 스토리지 / Redis 영속화 중 무엇? 원본 파일 보관 주체는?
- **세션 연동**: 상위가 `GET /game-context` 인라인 전달(추천, 세션 무변경) vs `POST /sessions`가
  `project_id`로 KB 직접 참조(결합↑)?
- **파일 포맷·크기**: v1 지원 포맷(pdf/txt/md?), 최대 크기, 스캔 PDF OCR 필요 여부.
- **overview 다중 문서 충돌**: fill-gaps 최초우선으로 충분한가, 아니면 primary 문서 지정이 필요한가.
- **집계 캐싱**: 조회 시 계산 vs 캐시+무효화 — 단순성/성능 트레이드오프.
