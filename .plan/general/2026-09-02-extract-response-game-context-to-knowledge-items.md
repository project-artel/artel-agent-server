# 2026-09-02 — extract 응답의 game_context 를 knowledge 항목 목록으로 바꾼다

- Date: 2026-09-02
- Jira: ARTEL-745
- Status: Implemented

## Goal

`POST /extract` 의 `ExtractResponse.game_context` 를 지금의 `GameContext` 객체
하나에서, `tag`/`summary`/`description` 세 필드를 가진 항목의 배열로 바꾼다.
orchestration-server 의 `AgentExtractClient` 가 `List<KnowledgeIngestItem>` 으로
역직렬화하기 때문에, 지금 형태로는 매 문서 업로드가
`Cannot deserialize value of type ArrayList<KnowledgeIngestItem> from Object
value (token JsonToken.START_OBJECT)` 로 실패하고 `parse_status` 가 `FAILED` 로
남는다.

## Non-goals

- `GameContext` 스키마 변경 — 그대로 둔다.
- `game_context/v1` 프롬프트, `prompts-lock.json` 변경 — 건드리지 않는다.
- 프롬프트가 `tag` 를 직접 붙이게 하는 것 — 변환은 코드에서 한다.
- 재추출 트리거.
- orchestration-server 쪽 계약 테스트 — 별도 작업.

## Context / Constraints

- 지금 응답 형태: `app/api/extract.py:26` 의 `ExtractResponse.game_context:
  GameContext` (`app/agents/game_context/schemas.py:68`).
- `tag` 어휘는 이미 `app/agents/qa/knowledge.py:120` 의
  `KNOWLEDGE_TAGS = ("CONTROL", "RULE", "OBJECTIVE", "UI", "MISC")` 에 있다.
  새로 정의하지 않고 그대로 가져다 쓴다.
- `ExtractionService.extract` (`app/documents/service.py`) 는 지금처럼
  `GameContext` 를 돌려준다. 변환은 API 경계, 즉 `app/api/extract.py` 의 라우트
  핸들러에서 한다.
- 변환 함수는 LLM 없이 단위 테스트할 수 있도록 순수 함수로 따로 둔다. 새 모듈
  `app/api/game_context_knowledge.py` 를 만든다 — `app/api/` 아래 리소스별
  모듈이 이미 있는 관례(`embeddings.py`, `knowledge_queries.py`, `extract.py`)를
  따른다.
- 새 스키마 이름은 orchestration 쪽이 읽는 타입 이름을 그대로 따라
  `KnowledgeIngestItem` 으로 한다. 기존 `app/agents/knowledge_query/schemas.py`
  의 `KnowledgeItem` 은 `id`/`summary`/`description` 만 가진, 검색 질의 생성용의
  다른 개념이라 재사용하지 않는다.
- `GameContext` 의 여덟 section 전부가 변환을 거친다. `summary` 나
  `description` 이 빈 문자열이 되는 항목만 내보내지 않는다.

## 변환 규칙 (Jira ARTEL-745 그대로)

section 마다 고정된 `tag`. `summary` 는 그 항목의 이름, `description` 은 나머지
필드를 줄바꿈으로 이어 붙인다. 각 필드는 `label: value` 형태의 한 줄로 넣는다
(비어 있으면 그 줄은 스킵). 리스트 필드(`elements`, `rules`, `steps` 등)는 쉼표로
이어 하나의 줄로 만든다.

- `overview` (단일 객체) — tag `OBJECTIVE`. summary=`title`,
  description 줄: `genre`, `platform`, `summary`, `core_loop`
- `screens` — tag `UI`. summary=`name`,
  description 줄: `purpose`, `elements`, `transitions`
- `mechanics` — tag `RULE`. summary=`name`,
  description 줄: `description`, `rules`, `preconditions`
- `entities` — tag `MISC`. summary=`name`, description 줄: `type`, `attributes`
- `progression` — tag `OBJECTIVE`. summary=`name`, description 줄: `order`, `notes`
- `flows` — tag `CONTROL`. summary=`name`, description 줄: `steps`
- `glossary` — tag `MISC`. summary=`term`, description 줄: `meaning`
- `misc` — tag `MISC`. summary=`note` 의 앞부분(첫 줄, 최대 80자),
  description=`note` 전체

`flows` 를 `CONTROL` 로 보내는 것이 유일하게 논쟁의 여지가 있다고 Jira 이슈가
밝히고 있다 — flow 는 플레이어가 밟는 단계 순서라 `RULE` 이나 `UI` 보다
`CONTROL` 에 가깝다고 본 것. 상수 하나라 리뷰에서 뒤집기 쉽다. 이 근거를 변환
함수 옆 주석으로도 남긴다.

### 빈 값 판정 규칙 (fast review 반영)

- `summary` 나 `description` 이 `None` 이거나 `""` 면 그 필드는 "비었다" 로
  본다. 둘 중 하나라도 비면 그 항목은 내보내지 않는다 — Jira 원문 "summary 나
  description 이 비면 내보내지 않는다" 그대로.
- `description` 을 이루는 각 줄(`label: value`) 은, 문자열 필드는 `None` 이거나
  `""` 면 그 줄을 통째로 스킵하고, 리스트 필드(`elements`, `rules`,
  `preconditions`, `attributes`, `steps`, `transitions`)는 빈 리스트면 그 줄을
  스킵한다. 리스트는 값을 쉼표로 이어 한 줄로 만든다.
- `overview` 가 `None` 이면 `OBJECTIVE` 항목을 하나도 만들지 않는다 — 다른
  section 처럼 리스트가 아니라 단일 객체라 이 경우를 명시한다.
- `misc.note` 의 summary 는 `note` 를 `\n` 기준으로 나눈 첫 줄을, 80자
  (Python 문자열 길이, 유니코드 문자 수 기준 — 한글도 1자) 를 넘으면 그만큼만
  자른 것. description 은 `note` 전체.
- `progression.order` 는 `int | None` 이다(`ProgressionItem.order`,
  `app/agents/game_context/schemas.py`). 숫자 필드는 `None` 일 때만 그 줄을
  스킵한다 — `0` 은 유효한 값이므로 `order: 0` 줄로 남긴다. 값은 `str()` 로
  렌더한다 (heavy review 지적 반영).

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료. `app/api/extract.py`, `schemas.py`,
      `app/agents/qa/knowledge.py`, `app/documents/service.py`,
      `tests/test_extract.py` 를 읽었다.
- [x] **Step 1: 변환 모듈** — `app/api/game_context_knowledge.py` 새로 작성.
      - `KnowledgeIngestItem(BaseModel)`: `tag: str`, `summary: str`,
        `description: str`.
      - 공유 헬퍼 하나: `_join_fields(*pairs: tuple[str, str | int | None |
        list[str]]) -> str` — 각 pair 를 `label: value` 줄로 만들되, 문자열은
        `None`/`""`, 리스트는 빈 리스트일 때만 그 줄을 스킵하고, 숫자는 `None`
        일 때만 스킵한다(`0` 은 유효한 값이라 남긴다. `str()` 로 렌더).
        리스트는 값을 쉼표로 이어붙인다. 남은 줄을 `\n` 으로 합친다. 여덟
        section 함수가 전부 이 헬퍼를 불러 쓰고, `label: value` 조립 로직을
        각자 다시 짜지 않는다 (medium review DRY 지적 반영, 숫자 케이스는
        heavy review 지적 반영).
      - section 별 변환을 작은 private 함수로 나눠 각 section 규칙이 한 곳에
        있게 한다. `overview` 는 리스트가 아니라 단일 객체이므로 `None` 이면
        빈 리스트를 돌려준다.
      - tag 값은 `KNOWLEDGE_TAGS` 의 멤버 문자열을 그대로 적어 넣는다(새
        어휘를 만들지 않는다는 제약을 지키는 것은 이 값들이 `KNOWLEDGE_TAGS`
        의 부분집합이라는 사실이지, 상수를 프로그램적으로 다시 끌어오는 것이
        아니다 — Step 3 의 테스트가 이 사실을 고정한다).
      - `game_context_to_knowledge_items(context: GameContext) ->
        list[KnowledgeIngestItem]` 은 위 여덟 함수를 순서대로 호출해 이어붙이고,
        `summary` 나 `description` 이 빈 문자열인 항목을 걸러낸다.
- [x] **Step 2: API 경계 배선** — `app/api/extract.py`:
      - `ExtractResponse.game_context` 의 타입을
        `list[KnowledgeIngestItem]` 로 바꾼다 (필드 이름은 유지 — orchestration
        의 `AgentExtractClient` 가 이 필드명으로 읽는다).
      - 라우트 핸들러에서 `ExtractionService.extract` 가 돌려준 `GameContext`
        를 `game_context_to_knowledge_items` 로 변환한 뒤 응답에 담는다.
      - `ExtractionService.extract` 시그니처/리턴 타입은 그대로 둔다.
- [x] **Step 3: 테스트**
      - 새 파일 `tests/test_game_context_knowledge.py`:
        - 여덟 section 각각의 매핑 (한 section 에 값을 채운 `GameContext` 하나
          넣고 tag/summary/description 확인).
        - 빈 section(리스트가 `[]`, `overview` 가 `None`) 은 항목을 만들지
          않는 것.
        - `summary` 나 `description` 이 (원본 필드가 전부 `None`/`""`/빈
          리스트라서) 비게 되는 항목이 걸러지는 것.
        - 빈 `GameContext()` 전체가 빈 리스트를 내는 것.
        - `misc.note` 가 80자보다 긴 한 줄일 때 summary 가 80자로 잘리고
          description 은 전체를 담는 것, `note` 에 개행이 있어 첫 줄이 80자
          보다 짧을 때 그 첫 줄만 summary 가 되는 것.
        - 리스트 필드가 여러 값을 담을 때 쉼표로 이어지는 것 (예: `screens`
          의 `elements`).
        - `progression` 에서 `order=0` 일 때 그 줄이 스킵되지 않고
          `order: 0` 으로 남는 것 (heavy review 지적 반영 — `0` 을 falsy 로
          취급해 지우는 실수를 막는다).
        - 반환되는 모든 항목의 `tag` 가 `KNOWLEDGE_TAGS` 의 원소인 것 (medium
          review 지적 반영 — 어휘가 두 곳에서 따로 관리되지 않는다는 것을
          고정).
        - `set(GameContext.model_fields) == {"overview", "screens",
          "mechanics", "entities", "progression", "flows", "glossary",
          "misc"}` 를 고정하는 테스트 하나 (medium review 지적 반영 — 나중에
          `GameContext` 에 아홉 번째 section 이 추가되면 이 변환 함수가 그
          section 을 조용히 빠뜨리는 대신 이 테스트가 먼저 깨지게 한다).
      - `tests/test_extract.py` 의 `test_extract_route_returns_game_context`
        갱신. 지금 fixture `GameContext(overview=Overview(title="WordVenture"))`
        는 `title` 외 모든 `Overview` 필드가 `None` 이라 description 이 비고
        항목이 걸러져 `body["game_context"]` 가 빈 리스트가 되어 검증이
        공허해진다(heavy review 지적). fixture 를
        `Overview(title="WordVenture", genre="word puzzle")` 로 채워, 응답이
        정확히 `[{"tag": "OBJECTIVE", "summary": "WordVenture", "description":
        "genre: word puzzle"}]` 인 것을 고정한다.
      - `LANGSMITH_TRACING=false .venv/bin/python -m pytest` 전체 통과 확인.

## Validation

- **Commands to run:**
  `LANGSMITH_TRACING=false .venv/bin/python -m pytest`
- **Expected output:** 전체 테스트 통과, 새 테스트 포함.
- **Manual check (parent agent):** 로컬 stack 에 기획서 한 건을 올려
  `parse_status` 가 `EXTRACTED` 로 바뀌고 `knowledge` 행이 생기는 것을 확인 —
  이 worktree 는 서버를 띄우지 않으므로 검증은 parent agent 가 agent-server 를
  이 branch 로 재시작한 뒤 수행한다.

## Risks & Rollback

- **Risks:** `flows` → `CONTROL` 매핑이 리뷰에서 뒤집힐 수 있다 (상수 하나라
  변경 비용은 낮다). `misc.note` 앞부분 자르는 길이(80자)는 발명한 값이라
  리뷰에서 조정될 수 있다.
- **Rollback steps:** 이 PR 을 revert 하면 `ExtractResponse.game_context` 가
  이전 `GameContext` 객체 형태로 돌아간다. 스키마나 프롬프트를 건드리지 않았으므로
  revert 에 부수 효과가 없다.

## Open Questions

- (없음)

## Plan Review Findings

Fast (haiku) 와 medium (sonnet) 리뷰를 first pass 로 돌렸다.

**반영:**
- 빈 값 판정(`None`/`""`/빈 리스트) 규칙을 명시 — "### 빈 값 판정 규칙" 절 추가.
- `misc.note` 잘라내는 방식을 "첫 줄, 80자 넘으면 자름" 으로 명확히 함.
- `overview` 가 `None` 일 때 항목을 안 만드는 것을 명시.
- 리스트 필드가 빈 리스트일 때 그 줄을 스킵하는 것을 명시.
- 공유 헬퍼 `_join_fields` 를 Step 1 에 추가해 여덟 section 함수가 `label:
  value` 조립 로직을 각자 다시 짜지 않게 함 (DRY).
- tag 값이 `KNOWLEDGE_TAGS` 의 부분집합이라는 것을 테스트로 고정 — 어휘가
  두 곳에서 따로 관리되지 않는다.
- `GameContext` section 집합을 테스트로 고정해, 아홉 번째 section 이 생기면
  변환 함수가 아니라 테스트가 먼저 깨지게 함.
- `flows` → `CONTROL` 근거를 코드 주석으로도 남기기로 함.

Heavy (opus) 리뷰를 두 번 돌렸다. 첫 pass 는 NONPASS — blocker 둘: (1)
`_join_fields` 와 빈 값 판정 규칙이 `progression.order: int | None` 을 다루지
않아 `order=0` 이 falsy 로 지워질 위험, (2) `test_extract_route_returns_
game_context` 의 fixture 가 `title` 만 채워 변환 후 항목이 걸러지고 검증이
빈 리스트를 공허하게 통과하는 문제. 둘 다 반영(위 "빈 값 판정 규칙" 절과
Step 3 fixture 서술)한 뒤 두 번째 pass 는 PASS.

## Pair Review Findings

`pair-review-critic` 역할로 구현을 리뷰한 결과: **VERDICT: PASS**. 여덟
section 전부가 변환을 거치는 것, `_join_fields` 가 str/list/int 의 "비었다"
를 각각 옳게 판정하는 것(특히 `order=0` 보존), `overview=None` 처리,
`flows→CONTROL` 주석, `GameContext.model_fields` 를 고정하는 테스트, DRY 하게
`_join_fields` 하나로 여덟 section 이 모이는 것을 모두 확인했다.

논블로킹 제안 하나 — `KnowledgeIngestItem.tag` 를 `str` 대신
`Literal["CONTROL", "RULE", "OBJECTIVE", "UI", "MISC"]` 로 두면 오타를
pydantic 생성 시점에 막을 수 있다는 것. **받아들이지 않음**: `Literal` 을
쓰려면 이 다섯 문자열을 코드에 다시 적어 넣어야 하는데, 이는 plan review
medium 단계에서 이미 다룬 "어휘를 두 곳에서 값으로 관리하지 않는다" 는
결정과 같은 트레이드오프다. `test_every_item_tag_is_a_known_knowledge_tag`
가 이미 그 계약을 테스트로 고정하고 있어, 추가 이득 대비 새로 얻는 것이 크지
않다고 보고 이번 변경 범위에 넣지 않았다.

**받아들이지 않음:**
- fast review 가 제안한 "80자는 바이트가 아니라 유니코드 문자 수" 각주는 본문에
  한 줄로 흡수했다 — 별도 섹션을 만들 정도는 아니라고 봤다.
- medium review 의 tag 어휘 관련 질문에 대해, `KNOWLEDGE_TAGS` 튜플의 인덱스나
  멤버를 직접 끌어와 매핑 dict 를 만드는 방안은 채택하지 않았다. section 이름과
  tag 문자열의 대응은 리터럴로 적는 편이 읽기 쉽고, "새 어휘를 만들지 않는다"
  는 제약은 테스트가 그 값들이 `KNOWLEDGE_TAGS` 의 부분집합임을 고정하는 것으로
  충분히 지켜진다.
