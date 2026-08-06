# 2026-08-06 — 지식 그래프 도구와 화면 지도 프롬프트

- Date: 2026-08-06
- Jira: (미발급)
- Status: Draft
- Base: `origin/develop` = `0eab2c0`
- 짝 문서: `artel-orchestration-server/.plan/general/2026-08-06-knowledge-edge-graph.md`
  (스키마·traversal·WS 계약은 전부 그쪽이 정한다. 여기는 Agent 쪽 절반이다.)

## Goal

Orchestration이 만든 지식 그래프를 QA 에이전트가 **쓰게** 한다.

1. 도구 셋 — `link_knowledge`(관계를 주장), `unlink_knowledge`(관계를 거둔다),
   `expand_knowledge`(그래프를 더 탄다).
2. 검색 결과에 딸려 오는 1홉 이웃을 렌더하고, 늘어난 전사를 접는다.
3. **화면 지도** — 시스템 프롬프트가 화면당 항목 하나와 `LEADS_TO` 전이를 짜도록 유도한다.

3번이 이 작업의 실질이다. 도구만 붙이고 프롬프트를 두면 에이전트는 링크를 거의 안 만들고,
그래프는 빈 채로 남아 Orchestration 쪽 작업이 통째로 헛돈다.

## Non-goals

- 스키마·traversal 질의·WS 라우팅. 전부 Orchestration.
- `knowledge_mode` 집행. 서버가 한다(ARTEL-256) — arm마다 Agent 프롬프트가 바이트 단위로 같아야
  하므로 Agent 쪽에는 모드 분기가 **없다**. 빈 검색 결과를 견디는 기존 처리로 충분하다.
- edge의 `note`를 고치는 도구. edge에서 고칠 수 있는 것은 `note`뿐이고, 틀렸으면 unlink 후
  다시 link하면 된다 — `update_knowledge`가 따로 있는 이유(본문이 길어 재입력이 비싸다)가 없다.
- v8 본문의 의미 변경. v9는 구조화 + 지식 절 추가이고 나머지 문장은 그대로 옮긴다.

## Context / Constraints

**선행 조건: Orchestration의 A·B가 머지돼 있어야 한다.** `link_knowledge`는 단방향이라
Orchestration이 `KNOWLEDGE_LINK`를 모르면 `appendError`만 남기고 **에이전트는 영영 모른다** —
성공했다고 보고하고 예산 한 칸을 태우고 아무것도 안 쓰인다. 성공처럼 보이는 조용한 무동작이
여기서 가능한 최악의 열화다.

**⚠️ 프롬프트 로더는 `versions[-1]`을 기본으로 쓴다**(`app/prompts/loader.py:228`, 숫자 정렬).
`qa_prompt_version`을 고정하지 않은 환경에서는 **v9 디렉터리가 생기는 순간 모든 런의 기본
프롬프트가 v9가 된다.** v9는 `link_knowledge`를 말하므로 **도구와 같은 PR에 들어가야 한다.**
프롬프트만 먼저 내보내면 없는 도구를 쓰라고 가르치는 프롬프트가 된다.

**ARTEL-192는 유지된다.** "툴 설명이 사용 정책의 단일 출처, 시스템 프롬프트는 안 건드림"에서
바뀌는 것은 없다 — 도구 하나를 **어떻게 부르는가**는 여전히 전부 도구 설명에 있다. v9의 새 절이
말하는 것은 `observe_scene` · `record_knowledge` · `link_knowledge`에 걸친 **작업 습관**이고,
그것은 어느 한 도구 설명에도 집이 없다. 습관을 도구 셋에 쪼개 넣으면 셋이 서로 어긋난다.

**`knowledge_seen`의 뜻을 약화시키지 않는다.** 아래 §`QaRunState` 참조 — 이 작업이 낼 수 있는
첫 회귀가 거기다.

## Approach (Checklist)

### 1. WS 계약 (`app/qa/`)

- [ ] `envelope.py`: `MessageType` += `KNOWLEDGE_LINK`(단방향), `KNOWLEDGE_UNLINK`(단방향),
      `KNOWLEDGE_EXPAND`, `KNOWLEDGE_EXPAND_RESULT`.
      라우터가 기대하는 철자 그대로, 단방향/응답 있음 주석 관례 유지.
- [ ] `envelope.py`: `KnowledgeLinkPayload`, `KnowledgeUnlinkPayload`, `KnowledgeExpandPayload`, `KnowledgeNeighbour`,
      `KnowledgeExpandResultPayload`. **전부 기본값 + `extra="allow"`** — 검증 실패가 기다리는
      도구를 20초 매달아 두기 때문이라는 기존 `KnowledgeSearchHit` 논거 그대로.
- [ ] `envelope.py`: `KnowledgeSearchHit` += `neighbors: list[KnowledgeNeighbour] = Field(default_factory=list)`.
      Orchestration이 먼저 나가도 이 필드가 없던 시절의 응답이 그대로 파싱된다.
- [ ] `channel.py`: `expand_knowledge()` — `search_knowledge()`를 본뜨되 **세 번째 waiter**를 쓴다.
      공유하지 않는 이유는 기존 주석대로: 지식 검색과 게임 액션이 서로의 future를 풀면 안 된다.
      같은 20초, 같은 3결과(`payload | KnowledgeSearchFailed | None`).
      `link_knowledge`는 기존 단방향 `write_knowledge()`를 탄다.
- [ ] `service.py` ~L178: `KNOWLEDGE_EXPAND_RESULT` 디스패치 분기.

### 2. 예산과 구조 (`app/agents/qa/arch.py`)

- [ ] `MAX_LINKS_PER_RUN = 3`, `MAX_UNLINKS_PER_RUN = 2`, `MAX_EXPANDS_PER_RUN = 3`.
      `QaArchSpec`(`ge=0, le=50`) · `ResolvedArch` · `tool_call_limit` 기본 합에 더한다 —
      셋 다 런 단위 허용량이라 스텝당이 아니라 기본에 속한다(검색에 대한 기존 KDoc 논거 그대로).

      사다리: 기존이 캡처 12 > 검색 6 > 기록 5 > 삭제 2다. link은 파괴적이지 않지만 하나하나가
      지속되는 주장이라 기록(5)보다 아래인 3. expand는 읽기이고 검색 뒤에만 쓸모가 있으며
      자동 1홉이 흔한 경우를 이미 덮으므로 검색(6)보다 아래인 3.
      unlink는 삭제 계열이라 `MAX_FORGETS_PER_RUN`과 같은 2 — 다만 문턱의 **이유**는 다르다.
      항목 삭제는 지식을 잃지만 링크 삭제는 연결 하나와 `note` 하나를 잃을 뿐이다. 같은 수인 것은
      파괴력이 같아서가 아니라 **둘 다 조용해서**다(지운 경로는 그냥 없어지고 아무도 안 물어본다).
- [ ] `forgets_need_records`와 나란한 새 validator 둘:

```python
@model_validator(mode="after")
def links_need_searches(self) -> "QaArchSpec":
    """검색이 보여준 id만 링크할 수 있으므로, 검색 없이 링크만 허용한 spec은
    법적으로 호출될 수 없는 도구를 켜 둔 것이다."""

@model_validator(mode="after")
def unlinks_need_searches(self) -> "QaArchSpec":
    """이웃은 검색에 딸려 오므로 같은 이유다. `forgets_need_records`와 달리
    unlink는 짝이 되는 쓰기를 요구하지 않는다 — 링크를 거두는 것은 그 자리를
    무엇으로 메워야 하는 일이 아니다."""
```

- [ ] **핑거프린트**: `ResolvedArch` 필드와 `build_tools`의 도구가 늘면 `arch_fingerprint`가 움직인다.
      구조가 실제로 바뀌었으니 맞다.
      `QA_ARCH_LABEL`은 **올리지 않는다** — 구조의 *종류*는 그대로다(여전히 `create_agent` 도구 루프).
      `_FINGERPRINT_SCHEME`도 **올리지 않는다** — 해시하는 사실의 집합은 그대로고 `arch`의 내용만 달라졌다.

### 3. 도구 산문과 렌더 (`app/agents/qa/knowledge.py`)

- [ ] 상수: `KNOWLEDGE_RELATIONS = ("LEADS_TO","REFINES","CONTRADICTS","DEPENDS_ON","REPLACES")`,
      `MAX_NEIGHBOUR_SUMMARY_CHARS = 120`, 이웃 블록 마커.
- [ ] `LINK_KNOWLEDGE_DESCRIPTION` — relation별 한 줄 판별법.
      **`LEADS_TO`의 `note`는 *왜*가 아니라 무엇을 했는지**다("마을 상단바의 상점 버튼") —
      경로를 나중 런이 쓸 수 있게 만드는 것이 그 문장이기 때문이다. 나머지 넷의 `note`는 왜다.
      그리고: `note`는 유일한 감사 기록이라 빈 문자열 불가; **넷… 다섯 중 맞는 것이 없으면
      링크하지 말 것**(거부한 `RELATED_TO`를 대신하는 문장); link은 지속되고 이후 모든 런이 읽는다;
      양끝이 이 런이 본 id여야 한다.
- [ ] `UNLINK_KNOWLEDGE_DESCRIPTION` — 문턱은 `forget_knowledge`보다 **낮다**(항목 둘은 살아남고
      잃는 것은 연결 하나와 `note` 하나). 그래도 조용하기는 마찬가지라 가르칠 것:
      화면이 **바뀐 것**과 화면이 **깨진 것**을 가릴 것 — 경로가 사라진 것처럼 보이는 이유는 대개
      빌드가 깨진 것이고 그것은 `report_issue`다; 한 번 안 됐다고 지우지 말 것;
      `LEADS_TO`는 조건부 경로일 수 있으니 `note`의 조건을 먼저 읽을 것("세이브가 있을 때만");
      지우는 이유를 `thought`에 쓸 것(유일한 기록).
- [ ] `EXPAND_KNOWLEDGE_DESCRIPTION` — 1홉 이웃은 검색과 함께 이미 왔으므로 이것은 더 가거나
      유사 항목을 보려는 것; `SIMILAR`는 기계의 짐작이고 타입 있는 다섯은 런이 이유를 적어
      주장한 것이라 같은 무게로 재면 안 된다.
- [ ] `RECORD_KNOWLEDGE_DESCRIPTION`에 **화면 항목을 초대하는 문단**을 더한다 —
      화면 하나 = 항목 하나, `UI` 태그, 무엇을 위한 화면이고 거기서 무엇을 할 수 있는가.
      **런 상태를 배제하는 기존 문장("플레이어가 골드 500을 갖고 있다")은 그대로 둔다** —
      화면 항목("상단바에 골드 카운터가 있고, 골드가 보이는 유일한 곳이다")과 런 상태
      ("골드 카운터가 340이다")를 가르는 선이 정확히 거기다.
- [ ] 렌더러 `render_neighbour` / `render_neighbours` / `render_expansion`.
      히트 안의 이웃 한 줄: `   ↳ [id 412 · contradicts] Purchases are blocked below the item price`.
      **접힌 줄에 `note`를 싣지 않는다** — 감사자의 필드이고 인라인하면 이웃당 비용이 두 배가 된다.
      expand 출력에서는 `note`를 찍는다(`LEADS_TO`는 거기서 경로가 쓸 수 있게 된다).
      벡터 이웃은 `~ [id 88 · similar 0.71] …`.

### 4. `QaRunState`와 도구 (`app/agents/qa/tools.py`)

- [ ] `knowledge_links_attempted`, `knowledge_expands_attempted`,
      **`knowledge_glimpsed: dict[str, str]`**(한 줄로 본 것. **pop하지 않는다**).
- [ ] `tools.py:391`의 `knowledge_seen` 채우는 루프가 `entry.neighbors`와 expand 결과의 이웃도
      훑어야 한다 — 아니면 에이전트는 보여 준 id를 아예 쓸 수 없다.

      **그런데 이웃은 `knowledge_glimpsed`에만 넣는다.** `knowledge_seen`의 취지는 "읽지 않은 것은
      고치거나 지울 수 없다"이고, 120자로 잘린 한 줄은 읽은 것이 아니다.
      `FORGET_KNOWLEDGE_DESCRIPTION`은 삭제를 "런에서 할 수 있는 가장 파괴적인 일"이라 부르는데,
      그 전제조건을 한 줄짜리로 낮추는 것이 **이 작업이 낼 첫 회귀**다.
      - `update_knowledge` / `forget_knowledge`는 계속 `knowledge_seen`을 요구한다. 거부 메시지에
        분기 하나 추가: glimpsed에는 있고 seen에는 없으면 *"이웃 줄로만 봤다 — 검색해서 전문을
        읽은 뒤 다시 부르라"*. 없으면 에이전트가 설명할 수 없는 거부를 맞는다.
      - `link_knowledge`는 **둘 중 아무 쪽**의 끝점이든 받는다 — 관계 주장은 파괴적이지 않고,
        두 항목이 모순인지 알기에는 요약으로 충분하다. `expand_knowledge`의 seed도 마찬가지.
      - 이름이 구분을 진다: `seen` = 전문을 읽음, `glimpsed` = 한 줄로 봄.
      - 부수 효과로 `CONTRADICTS` 이웃을 지우려면 검색 하나를 써서 읽어야 한다. 기능이다.
- [ ] 도구 둘. `_run`을 타지 않는다(게임을 건드리지 않는다 — 기존 지식 도구와 같은 이유):

```python
async def link_knowledge(step: int, thought: str, from_knowledge_id: str,
                         to_knowledge_id: str, relation: str, note: str) -> str
async def unlink_knowledge(step: int, thought: str, from_knowledge_id: str,
                           to_knowledge_id: str, relation: str) -> str
async def expand_knowledge(step: int, thought: str, knowledge_id: str, depth: int = 1) -> str
```

      `unlink_knowledge`는 edge id가 아니라 **`(from, to, relation)` 삼중조**를 받는다. 에이전트는
      edge id를 본 적이 없고(이웃 줄에 찍히는 id는 knowledge id다) 노출하면 도구 하나 때문에
      새 id 공간이 생긴다. `note`를 안 받는 것도 의도다 — 지우는 이유는 `thought`가 지고,
      그것이 이미 타임라인에 남는 유일한 기록이다.

      프레임이 나가기 **전에 로컬에서** 전부 검증한다. link은 단방향이라 Orchestration의 거부가
      내려오지 않기 때문이다: 예산 소진 → 남은 수와 함께 거부; `relation`이 `KNOWLEDGE_RELATIONS`에
      없음 → 거부; `note`가 빈 문자열 → 거부(반대편 `NOT NULL`은 프레임을 조용히 떨군다);
      끝점이 `knowledge_seen ∪ knowledge_glimpsed`에 없음 → 거부; `from == to` → 거부.
      `QaCancelled`는 재던지고 그 밖의 예외는 보고하고 런은 계속.
      `expand_knowledge`는 `search_knowledge`의 3결과 처리를 본뜨고 `depth`를 로컬에서 2로 자른다.

### 5. 접기 (`app/agents/qa/context.py`, `runner.py`)

- [ ] `render_results`가 이웃 블록을 마커로 감싼다:
      `<<knowledge neighbours for search {serial}>> … <</knowledge neighbours>>`.
      `SceneMemory.render`가 관측 번호를 다는 것과 똑같이 검색별 일련번호를 싣는다
      (지식 결과는 오늘 마커가 없다).
- [ ] `fold_stale_knowledge(messages, keep=1)` — `fold_stale_scenes`와 같은 계약: 순수,
      모델 입력 전용, 멱등, `ToolMessage.content`만, 안 바뀐 메시지는 같은 객체로 반환.
      placeholder는 접힌 id를 이름으로 부르고 `expand_knowledge`로 되찾을 수 있다고 말한다
      (마커 문법으로 짓지 않는 것은 `_placeholder`의 이유대로).
- [ ] `runner.py`: `middleware_names_for`의 `"fold_scene_views"` 바로 뒤에
      `"fold_knowledge_neighbours"`, `build_middleware`의 `builders`에 짝, 새
      `QaArchSpec.fold_stale_knowledge: bool = True`로 게이트. `fold_stale_scenes`와 나란해서
      하드코딩이 아니라 실험 축이 된다. 기존 미들웨어를 확장하지 않고 별도로 두는 것은 둘이
      독립적으로 꺼져야 하고 핑거프린트가 미들웨어 순서를 해시하기 때문이다.

**접는 것은 이웃 블록뿐이고 히트의 요약·본문은 절대 건드리지 않는다.** 선이 거기 있는 이유:
`fold_stale_scenes`가 장면을 접는 것은 게임이 움직였고 `observe_scene`이 되찾아 주기 때문이다.
지식 본문은 stale이 아니고(문서가 바뀐 게 아니다) 다시 읽으려면 6개뿐인 검색 예산을 쓴다 —
접으면 "귀한 자원을 써서 이 접기를 되돌리라"고 말하는 셈이라 장면 쪽보다 확연히 나쁜 거래다.
이웃 블록은 둘 다 반대다: **요청하지 않았는데 온 것**이고 **`expand_knowledge`로 정확히 되찾을 수
있다**(자기 예산이 따로 있다). 그래서 이 기능이 들여온 증가분만 딱 접히고,
`knowledge.py:74-77`이 적어 둔 기존 부채는 무관한 PR 안에서 조용히 갚아 버리지 않고 그대로 남는다.

**숫자.** 오늘 검색 한 번 = 5히트 × (헤더 ~60 + 요약 ~80 + 본문 ≤500) ≈ 3,200자 ≈ 900토큰이고
아무것도 안 접힌다 — 검색 6번이면 ~5,400토큰이 영구히 남는다. 자동 1홉(fanout 2 / 총 8)은
8줄 × (~26자 + 120자) ≈ **330토큰, 검색당 +37%**. 거부한 5히트 × fanout 3 = 15줄은 ~620토큰,
**+70%** — 총 상한을 순진한 `히트×fanout`이 아니라 8로 둔 이유다. `expand_knowledge`는
깊이 2 / fanout 3 / 20노드에 note까지 찍어 호출당 ~1,400토큰, 3번이면 4,200 — 진짜 위험은
여기이고 expand 예산을 3, 노드 예산을 20으로 둔 이유다.

`compaction.py`와의 관계: `SummarizationMiddleware`는 옛 메시지를 통째로 갈아치우므로 접힌 블록이
아예 요약되어 사라질 수 있다. 충돌이 아니다 — 접기는 모델 입력 전용이다. 다음 사람이 다시
알아내지 않도록 새 모듈 docstring에 한 문장 남긴다.

### 6. 프롬프트 v9 (`app/prompts/qa_run/v9/`) — **초안 작성됨**

`system.md`(작성 완료) + `vision_directive.md`(v8에서 복사). 본문의 나머지는 v8 그대로이고
바뀐 것 둘이다.

**(a) md 제목으로 구조화.** v8은 평평한 산문 21문단이다. v9는 그 문장들을 그대로 두고
`## How to work` / `## Reading what the game sends back` / `## Finding the step's target` /
`## State you set…` / `## A failed step does not end the run` / `## The knowledge base` /
`## When the game itself is broken` / `## When your context is compacted` / `## The operator`로 나눈다.

**(b) `## The knowledge base` 절 신설.** 소절 둘:

- **`### The screen map`** — 화면 하나당 항목 하나. 화면은 "플레이어가 있을 수 있고 거기서 행동할 수
  있는 곳"이고, 씬뿐 아니라 **액션 가능한 것을 바꾸는 패널·오버레이·다이얼로그·탭 각각**이다.
  마을 위의 상점 패널은 마을의 각주가 아니라 자기 화면이다 — 거기서 행동할 수 있고, 드나드는
  경로가 그 자체로 사실이기 때문이다. `UI` 태그.
  전이마다 `LEADS_TO` 하나, `note`에 **무엇을 했는지**. 왕복은 서로 다른 두 경로다 —
  나가는 길이야말로 나중 런이 막히는 지점이다.
  **실제로 걸어 본 것만 넣는다**: 런의 쓰기(5)와 링크(3) 예산은 한 줌이고, 안 가 본 화면을
  지도에 채우는 런은 지도를 지어내면서 시험을 멈춘 것이다. 지도는 런들에 걸쳐 조립된다.
  이미 있으면 그것도 소득이다 — 지도가 맞았고 다음에 믿어도 된다는 뜻이다.
- **`### Structuring the rest of what you know`** — 화면 지도는 가장 선명한 사례이지 유일한
  사례가 아니다. `REFINES`(구체적 → 일반), `DEPENDS_ON`(선행조건),
  `CONTRADICTS`(**가장 값지고 가장 기록 안 되는 것** — 알아차리는 순간이 보통 둘 중 무엇을
  믿을지 고민하느라 바쁜 순간이라서), `REPLACES`. 그리고 **"그 밖에는 링크하지 말 것"** —
  대충 같은 주제라는 것은 관계가 아니고(검색이 이미 그것을 찾는다) 아무 말도 안 하는 링크가
  말을 하는 링크를 밀어낸다.
- **`### Removing a link`** — `unlink_knowledge`. 피해야 할 실수 하나가 절의 중심이다:
  **빌드가 깨졌다고 링크를 지우는 것.** 안 열리는 문은 없어진 경로보다 버그일 때가 훨씬 많고,
  그것은 `report_issue`다 — 지우면 고장을 보고하는 대신 지도를 지운 것이고, 그 손해는 이후
  모든 런이 진다. `LEADS_TO`의 조건부 `note`("세이브가 있을 때만")도 같은 함정이다.

`## When the game itself is broken`에 한 문단 더한다: **깨진 빌드는 지식창고를 다시 쓸 이유가
아니다.** 항목과 본 것의 불일치 하나는 stale 지식보다 버그일 때가 많다. 기존 도구 설명이
말하던 것을 절 수준으로 올린 것이고, 화면 지도가 생기면 이 실수의 표면적이 넓어지기 때문이다
(화면이 바뀐 것과 화면이 깨진 것을 가려야 한다).

## Validation

```bash
cd /home/yunseong/dev/artel/.worktrees/agent-knowledge-edge && python -m pytest
```

- `test_qa_knowledge.py`(확장) — 이웃이 히트 아래 렌더된다; 120자에서 잘린다;
  이웃이 `knowledge_glimpsed`에 들어가고 `knowledge_seen`에는 **안 들어간다**;
  `forget_knowledge`가 glimpsed 전용 id를 **"전문을 읽으라"는 메시지와 함께** 거부한다;
  `link_knowledge`가 glimpsed 끝점을 받는다; link의 예산·미지 relation·빈 note·자기링크·미열람
  거부와 전송 실패 후 런 계속; expand의 예산·타임아웃·`KnowledgeSearchFailed`·빈 결과;
  `LEADS_TO`가 `KNOWLEDGE_RELATIONS`에 있다;
  **unlink**의 예산·미지 relation·미열람 끝점 거부, 그리고 **`link`와 예산을 공유하지 않는다**
  (link 3개를 다 쓴 런도 unlink를 부를 수 있어야 한다 — 링크를 거두는 것은 링크를 만드는 것과
  같은 자원이 아니다).
- `test_qa_agents_context.py`(확장) — `fold_stale_knowledge`의 순수성·멱등성·최신 1개 유지·
  **히트 본문 불변**·지식 아닌 `ToolMessage` 불변.
- `test_qa_arch.py`(확장) — 새 spec/resolved 필드, `links_need_searches`,
  `tool_call_limit`에 새 허용량 반영, 핑거프린트 이동, `QA_ARCH_LABEL` **불변**.
- `test_qa_service_deliver.py`(확장) — `KNOWLEDGE_EXPAND_RESULT` 디스패치가 채널에 닿는다.
- `test_qa_prompt_version.py` / `test_prompts_loader.py`(확장) — v9가 목록에 있고 로드되며
  `{vision_directive}`·`{language_directive}`가 치환된다; **v9가 기본으로 뽑힌다**(`versions[-1]`);
  v8은 명시 지정으로 여전히 로드된다(비교 축이 살아 있어야 한다).

### End to end

지식이 있는 프로젝트로 실제 QA 런을 돌려, 에이전트가 (1) 화면을 `UI`로 기록하고
(2) 전이를 `LEADS_TO`로 링크하고 (3) 이후 검색이 그 이웃 줄을 내는지 본다.
프롬프트가 실제로 그 행동을 유도하는지는 **이 확인 전에는 알 수 없다** — 아래 Risks 1번.

## Risks & Rollback

**머지 순서: Orchestration A·B → 여기(도구 + v9 같은 PR) → 접기.**
v9만 앞세우지 않는다(기본 프롬프트가 조용히 바뀌고 없는 도구를 가리킨다).
접기는 얼마든지 늦어도 된다 — 없으면 이웃 블록이 안 접힐 뿐이다.

**롤백**: 도구는 `build_tools`에서 빼고 `qa_prompt_version=v8`을 고정하면 즉시 이전 동작이다.
스키마 롤백은 필요 없다 — Agent 쪽은 상태를 안 들고 있다.

### 확신 없는 것들

1. **프롬프트가 실제로 화면 지도를 만들게 하는지는 실제 런 전에는 모른다.** 이 작업 전체에서
   가장 큰 미지수이고, 단위 테스트로는 닿지 않는다. 첫 실측에서 봐야 할 것: 런당 기록된 `UI`
   항목 수, 런당 `LEADS_TO` 링크 수, 그리고 **에이전트가 지도를 만드느라 시험을 덜 했는지**
   (스텝당 도구 호출 수, 완료 스텝 비율). 후자가 나빠지면 프롬프트의 "실제로 걸어 본 것만"이
   약한 것이므로 문구를 벼린다.
2. **`MAX_LINKS_PER_RUN = 3`으로 화면 지도가 자라는 속도.** 런당 3개면 의미 있는 크기가 되기까지
   수십 런이 걸린다. `LEADS_TO`에만 별도 허용량을 줄지는 1번 실측 뒤에 정한다 — 지금 나누면
   있지도 않은 문제에 예산 축을 하나 더 만드는 것이다.
3. **`MAX_LINKS_PER_RUN` / `MAX_EXPANDS_PER_RUN = 3`**은 기존 사다리에서 추론한 것이지 측정한
   것이 아니다. `QaArchSpec` 필드로 두는 이유가 이것이다.
4. **relation 다섯을 잘 가려 쓸지 모른다.** `REFINES`나 `LEADS_TO`가 전부 삼키면
   `LINK_KNOWLEDGE_DESCRIPTION`을 벼리는 것이 답이지 값을 더하는 것이 아니다.
5. **v9가 기본 프롬프트를 갈아치운다.** 프롬프트 버전은 V25(ARTEL-239)의 비교 축이기도 하므로,
   v8↔v9 점수를 견주려면 **양쪽을 명시적으로 고정해** 돌려야 한다. 기본값에 맡기면 전부 v9다.
6. **화면 항목을 `UI` 태그에 얹는 것으로 충분한지.** V15 주석은 "한 축이 모자라면 enum 값을 늘리지
   말고 직교 facet 컬럼을 더하라"고 못박아 뒀다. 화면과 그냥 UI 규칙을 갈라야 한다면 그것이
   그 facet의 첫 소비처가 되고, 그때는 Orchestration 쪽 마이그레이션이다.

## Open Questions

- 도구 + v9를 한 PR로 낼지, 도구만 먼저 내고 v9를 그다음 PR로 내되 그 사이
  `qa_prompt_version=v8`을 고정해 둘지. 후자가 리뷰 단위는 작지만 설정 고정을 잊으면 1번 위험이
  그대로 실현된다.
- v9의 화면 지도 문구를 처음부터 전량 넣을지, 화면 지도만 넣고 "그 밖의 지식 구조화" 소절은
  다음 버전으로 미뤄 프롬프트 변경 하나당 변수 하나로 좁힐지.
