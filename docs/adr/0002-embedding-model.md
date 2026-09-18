# ADR 0002 — embedding 을 text-embedding-3-large 1024 차원으로 정한다

- 상태: 확정
- 근거: `app/config.py` (`embedding_model`, `embedding_dimensions`, `embedding_base_url`),
  `app/llm/embedding_model.py`,
  artel-orchestration-server 의 `V18__create_knowledge_embedding.sql`,
  Notion 「QA Agent 임베딩 모델 선정 근거 (ARTEL-184)」
- 이 결정의 측정 기록은 이 저장소에 없고 위 Notion 페이지에만 있습니다.

## 결정

- 검색용 embedding 은 `openai/text-embedding-3-large` 를 **1024 차원으로 잘라** 씁니다.
- embedding 자격증명은 chat 과 따로 둘 수 있습니다 (`EMBEDDING_BASE_URL`,
  `EMBEDDING_API_KEY`). 비면 chat 것을 그대로 씁니다.

## 왜

### 1. 평가셋을 두 번 만들었습니다

- 처음에는 주제가 서로 다른 한국어 knowledge 항목 12 개로 만들었습니다.
  - **후보 여섯이 전부 top-1 1.000 으로 포화**해서 아무것도 가르지 못했습니다.
  - 서로 다른 주제를 구분하는 것은 어떤 모델에게도 쉬운 일입니다.
- 그래서 다시 만들었습니다. 같은 게임 시스템 안에서 규칙만 다른 **근접 distractor** 를
  채운 34 항목과 구어체 한국어 검색어 34 개입니다.
- 실제 검색에서 헷갈리는 것은 「상점 구매 조건」과 「길드 가입 조건」이 아니라
  「구매 조건」과 「판매 조건」 같은 것들이기 때문입니다.

### 2. 평균이 아니라 최소가 갈랐습니다

`margin` 은 정답과 최상위 오답의 코사인 거리 차이입니다.

| 후보 | 차원 | top-1 | MRR | margin 평균 | margin 최소 |
| --- | --- | --- | --- | --- | --- |
| `openai/text-embedding-3-large` | 3072 | 34/34 | 1.0000 | +0.1224 | **+0.0100** |
| `voyageai/voyage-4-large` | 1024 | 33/34 | 0.9853 | **+0.1514** | −0.0289 |
| `qwen/qwen3-embedding-8b` | 4096 | 32/34 | 0.9706 | +0.1290 | −0.0057 |
| `google/gemini-embedding-001` | 3072 | 32/34 | 0.9706 | +0.0980 | −0.0249 |
| `intfloat/multilingual-e5-large` | 1024 | 31/34 | 0.9559 | +0.0321 | −0.0098 |
| `baai/bge-m3` | 1024 | 31/34 | 0.9559 | +0.0890 | −0.0448 |

- **최소 margin 이 양수인 후보는 하나뿐입니다.** 나머지는 어딘가에서 오답이 정답을
  앞질렀다는 뜻입니다.
- `voyage-4-large` 는 **평균 margin 이 1 위인데도** 여기서 갈렸습니다. 평균만 보면
  놓치는 것이 정확히 이 자리입니다.

### 3. 1024 는 모델이 아니라 pgvector 때문입니다

- `text-embedding-3-large` 의 native 차원은 3072 입니다. 1024 는 그것을 자른 값입니다.
- **pgvector 의 HNSW 와 IVFFlat 인덱스는 2000 차원이 상한입니다.** 3072 이나 4096 을
  그대로 쓰면 지금 인덱스를 안 만들더라도 나중에 붙이려는 순간 `halfvec` 우회를 떠안습니다.
- Matryoshka 표현이라 잘라도 잃는 것이 없었습니다 — **1024 에서도 34/34, MRR 1.0** 으로
  원본과 같았습니다.
- 이 판단은 실제로 값을 했습니다. 인덱스 상한을 모르고 4096 짜리를 골랐다면 스키마를
  다시 뒤집어야 했을 것입니다.
- 지금 HNSW 인덱스는 일부러 안 만듭니다. 프로젝트당 knowledge 가 몇백~몇천 행이라
  순차 스캔이 밀리초이고 `project_id` 필터가 먼저 걸립니다. 느려지면 그때
  `CONCURRENTLY` 로 온라인 생성하면 되고, **차원을 1024 로 맞춰 뒀으므로 그때 상한에
  걸리지 않습니다.**

### 4. embedding 자격증명이 chat 과 따로인 이유

- chat 은 비용 때문에 다른 gateway 나 provider 로 옮겨도 됩니다 — Bedrock, 프록시,
  두 번째 계정.
- **embedding 은 함께 옮기면 안 됩니다.** `embedding_dimensions` 가 orchestration 의
  `vector(N)` 컬럼에 못 박혀 있고, 다른 모델은 그 컬럼을 **다른 vector space** 로 채웁니다.
- 그러면 저장된 항목과 새 항목이 **오류 없이 틀린 답으로** 비교됩니다. 검색이 아예 없는
  것보다 나쁩니다.
- 실제 선언은 `vector(1024)` 이고 테이블 둘이 씁니다 — `knowledge_embedding` (V18) 과
  `test_case_embedding` (V23).

## 거절한 대안

| 대안 | 왜 거절했나 |
| --- | --- |
| `voyage-4-large` | margin 평균 1 위지만 최소가 −0.0289. 어딘가에서 오답이 이김 |
| `qwen3-embedding-8b` | 최소 margin 음수, 게다가 4096 차원이라 인덱스 상한을 넘음 |
| `gemini-embedding-001` | 최소 margin 음수, 3072 차원 |
| `multilingual-e5-large` | top-1 31/34, margin 평균도 +0.0321 로 가장 낮음 |
| `bge-m3` | top-1 31/34, 최소 margin −0.0448 로 가장 나쁨 |
| native 3072 을 그대로 씀 | pgvector 인덱스 2000 차원 상한. 나중에 `halfvec` 우회를 떠안음 |
| 12 항목 평가셋 | 후보 여섯이 전부 top-1 1.000 으로 포화. 변별력이 없음 |
| embedding 자격증명을 chat 과 공유만 함 | chat 이 gateway 를 옮기면 vector space 가 조용히 갈림 |

## 대가

| 대가 | 무엇이 일어나나 |
| --- | --- |
| `vector(1024)` 가 되돌리기 비쌈 | 모델이나 차원을 바꾸면 마이그레이션과 전체 re-index 가 따라옴 |
| 평가셋이 지어낸 가상 게임 문서 | 실제 `knowledge` 테이블 데이터로는 재지 못했음 |
| 34 개는 통계적으로 작음 | 표의 차이는 표본이 커지면 뒤집힐 수 있음 |
| 측정 기록이 코드 밖에 있음 | 근거 전문은 Notion 에만 있고, 저장소에는 `app/config.py` 의 주석 한 문단뿐 |
| 지금 인덱스가 없음 | 규모가 커지면 순차 스캔이 먼저 느려짐. 대비는 차원을 맞춰 둔 것뿐 |
