# game_context KB — orchestration 연동 핸드오프

- Date: 2026-07-22
- For: orchestration-server 담당
- From: agent-server (game_context 추출기 구현 완료)

## 한 줄 요약

기획서 등 문서를 프로젝트에 올리면 → agent-server가 **구조화 game_context로 추출** →
orchestration이 **저장·재사용(dedup)·병합** → QA 세션이 그 game_context를 공유한다.
**agent-server는 무상태 추출기**이고, **저장/재사용/집계/세션 주입은 orchestration 몫**이다.

## 책임 경계

| 기능 | 소유 |
|---|---|
| 문서 → game_context 추출 (파싱 + LLM) | **agent-server** (무상태, 이미 구현) |
| S3 저장, presigned URL 발급 | orchestration |
| sha256 해시 · **dedup 판단** | orchestration |
| DocumentStore (durable DB) | orchestration |
| 문서 병합(Aggregator) → game_context | orchestration |
| 세션 오픈 시 game_context 주입 | orchestration |

## agent-server가 제공하는 계약 (orchestration이 호출)

```
POST /extract      (무상태 · 저장 안 함 · 호출 시마다 LLM 과금)

Request  { "source_url": "<presigned S3 GET URL>",
           "filename":   "wordventure.pdf",
           "model":      "openai/gpt-4o-mini"   // optional, 기본값 있음
         }

Response { "filename":     "wordventure.pdf",
           "game_context": { ...아래 스키마... } }

Error    HTTP 4xx/5xx + { code, detail }   // fetch 실패 / 추출 실패 등
```

- agent-server가 `source_url`을 직접 fetch → 파싱 → 추출. presigned **GET** URL이면
  agent-server에 AWS 자격증명 불필요.
- **무상태**라 같은 URL을 두 번 보내면 LLM을 두 번 호출(재과금) → dedup은 orchestration 책임(아래).

### game_context 스키마 (agent-server 출력 형태)

고정 top-level 8섹션, 게임별 다양성은 엔트리 내부로. 문서 1개분이라 `source` 없음(병합 시 부여).

```
overview      { title, genre, platform, summary, core_loop }
screens[]     { name, purpose, elements[], transitions[] }
mechanics[]   { name, description, rules[], preconditions[] }
entities[]    { name, type, attributes[] }
progression[] { name, order, notes }
flows[]       { name, steps[] }
glossary[]    { term, meaning }
misc[]        { note }
```

## orchestration이 구현할 것

### 1) 인제스트 흐름 (dedup 포함 — 핵심)

```
업로드 바이트 수신
  → S3 저장 + presigned GET URL 발급
  → content_hash = sha256(바이트)                    # ★ LLM 호출 "전에" 계산
  → DocumentStore.get(project_id, content_hash)?
      ├ 히트 → 저장된 game_context 재사용, reused=true  # agent-server 호출 X (LLM 절약)
      └ 미스 → agent-server POST /extract {source_url, filename, model}
               → game_context
               → DocumentStore.save(project_id, content_hash, filename, game_context)
  → 응답 { document_id, content_hash, reused }
```

**dedup은 반드시 호출 전에.** 응답을 받고 나서 걸러도 LLM은 이미 쓴 뒤라 절약이 안 된다.
바이트가 처음 도착하는 orchestration이 해싱의 자연스러운 위치.

### 2) DB 스키마 (DocumentStore)

```
documents
  document_id    PK (uuid)
  project_id        (index; dedup 키의 일부)
  content_hash      (sha256)
  filename
  game_context      JSONB           # agent-server가 준 결과 그대로 (문서 1개분)
  created_at
  UNIQUE (project_id, content_hash)  # DB 레벨 dedup 강제
```

- 저장 대상은 **추출 결과**(원본 PDF 아님 — 원본은 S3).
- ⚠️ **presigned URL 저장 금지**(서명·시한부·민감). 원본 참조 필요하면 `s3_key`만.
- 선택 필드: `kind`, `model_used`, `extracted_text`(재집계용).

### 3) 집계 (Aggregator)

```
SELECT * FROM documents WHERE project_id = ?
  → 각 행의 game_context를 union 병합
  → 리스트 섹션(screens/mechanics/...): 엔트리 합치고 경량 dedup, 각 엔트리에 source=document_id 태깅
  → overview: created_at 순서로 빈 필드 채우기(fill-gaps)
  → 프로젝트의 game_context 완성
```

- v1은 **결정적 union**(LLM 화해 없음). 조회 시 계산 또는 캐시+무효화 중 택1.

### 4) 프로젝트 KB API (orchestration이 노출, 예시)

| 메서드 | 용도 |
|---|---|
| `POST /projects/{id}/documents` | 문서 등록 (위 인제스트 흐름) |
| `GET /projects/{id}/documents` | 등록 목록 |
| `DELETE /projects/{id}/documents/{document_id}` | 등록 해제 |
| `GET /projects/{id}/game-context` | 집계된 game_context (세션 주입용) |

### 5) 세션 연동 (agent-server 세션 계약 **무변경**)

```
세션 오픈 전:  GET /projects/{id}/game-context  → 집계 game_context
그대로 →  POST /sessions { unity_context, game_context, user_input, model? }
```

- 기존 `POST /sessions`는 이미 임의 dict `game_context`를 받으므로 **agent-server 변경 없음**.
  KB 출력을 그 슬롯에 꽂기만 하면 됨.

## 시퀀스

```
[문서 등록]
FE ─업로드─▶ orchestration
                ├ S3 저장 + presigned URL
                ├ sha256 → DocumentStore 조회
                │   ├ 히트 → 재사용 (agent-server 호출 X)
                │   └ 미스 → agent-server POST /extract {source_url,filename,model}
                │             └ game_context → DocumentStore.save
                └ {document_id, content_hash, reused}

[QA 세션]
orchestration ─ GET game-context(집계) ─▶ 자기 자신
orchestration ─ POST /sessions {game_context,...} ─▶ agent-server (기존 흐름)
```

## 보안 / 주의

- **presigned GET URL** 전달 (agent-server에 AWS 키 불필요).
- 크기 상한·URL 만료 시간 여유. 정상 버킷의 URL만 전달(agent-server도 호스트 가드 예정).
- dedup은 LLM 절약 위해 **호출 전 해시** 필수.

## 비목표

- agent-server 세션/시나리오 로직 변경 (없음).
- 멀티모달 추출, 대용량 문서 청킹 (agent-server 후속).

## 오픈 항목 (orchestration이 결정)

- durable 백엔드 (Postgres JSONB 권장) + 마이그레이션/백업.
- 파일 포맷·크기 정책 (agent-server v1 loader: pdf / txt / md).
- 집계 캐싱 (조회 시 계산 vs 캐시+무효화).
- overview 다중 문서 충돌 (fill-gaps 최초우선 vs primary 지정).
