# 2026-09-02 — 그래프 탐색 표기에 PART_OF 를 넣는다

- Date: 2026-09-02
- Jira: ARTEL-749
- Status: Implemented, pair-reviewed (PASS), tests green

## Goal

`app/agents/qa/knowledge.py` 의 `_REVERSED` 에 `PART_OF` 의 역방향 표기를 추가해서,
`expand_knowledge` 가 `PART_OF` edge 를 만났을 때 나가는 방향("이 항목은 저 문서의
일부다")과 들어오는 방향("이 문서는 저 항목을 담고 있다")이 서로 다른 문장으로
읽히게 한다. ARTEL-748 (orchestration-server) 이 지식 그래프에 `PART_OF` edge 를
추가하는 것에 대응하는 agent-server 쪽 슬라이스.

## Non-goals

- `PART_OF` 를 만드는 tool. 그 edge 는 적재 경로(ingest path)가 만든다.
- `KNOWLEDGE_RELATIONS` 에 `PART_OF` 를 추가하는 것 — agent 가 주장할 수 있는
  관계가 아니라 구조 관계이기 때문에 명시적으로 제외한다.
- 문서 node 를 특별 취급하는 검색 필터.

## Context / Constraints

- 관계 이름은 orchestration-server 가 정한 `PART_OF` 다. 방향은 항목(item)에서
  문서(기획서 node)로 향한다 — `from`(item) `--PART_OF-->` `to`(document).
- `render_neighbour` 는 `[hit 의 관계 · 이웃]` 을 "hit 이(가) [label] 이웃" 으로
  읽는다. `direction` 은 hit 이 어느 쪽에 있는지를 말한다: 기존 네 관계
  (`LEADS_TO`→"reached from", `REFINES`→"refined by", `DEPENDS_ON`→"required
  by", `REPLACES`→"replaced by")를 하나씩 대입해 보면, `direction == "OUT"`
  일 때 hit 은 `from` 쪽(관계를 만든 쪽)이라 기본 소문자 표기가 이미 맞고,
  `direction == "IN"` 일 때 hit 은 `to` 쪽(받는 쪽)이라 `_REVERSED` 의 표기가
  필요하다.
  - `PART_OF` 에서 `from`=item, `to`=document 이므로, hit 이 item 이면
    (`direction == "OUT"`) 이미 있는 소문자 fallback `part_of` 그대로 두어도
    "item 이(가) part_of 문서" 로 맞게 읽힌다 — `_REVERSED` 에 추가할 것이
    없다.
  - hit 이 document 이면(`direction == "IN"`) "문서가 [label] 항목" 이 "문서가
    항목을 담고 있다"는 뜻이 되어야 하므로 label 은 **`"contains"`** 다.
    처음 초안에 쓴 `"contained in"` 은 "문서가 항목에 담겨 있다"로 방향이
    거꾸로 읽혀 틀렸다 — fast review 에서 지적된 부분, `_REVERSED["PART_OF"]
    = "contains"` 로 확정.
- `_REVERSED` 위의 주석은 왜 `CONTRADICTS` 가 빠져 있는지, 그리고 왜
  `LEADS_TO` 는 `KNOWLEDGE_RELATIONS` 밖에 있으면서도 여기 남아 있는지를
  설명한다 — 같은 패턴을 `PART_OF` 에도 적용한다: 쓰기 어휘와 읽기 표기는
  분리되어 있고, `PART_OF` 는 애초에 agent 가 쓰는 관계가 아니다.
- `KNOWLEDGE_RELATIONS` 옆 주석에는 `PART_OF` 를 추가하지 않는 이유를 남긴다 —
  구조 관계이고 적재 경로가 만든다는 점, agent 가 주장할 수 있는 관계가 아니라는
  점.

## Approach (Checklist)
- [ ] **Step 0: Recon** — `app/agents/qa/knowledge.py` 의 `_REVERSED`,
  `KNOWLEDGE_RELATIONS`, `render_neighbour` 를 확인. `tests/test_qa_knowledge_graph.py`
  의 `LEADS_TO` 왕복 테스트(`test_a_stored_route_still_reads_back_in_both_directions`)
  를 표기 정하는 템플릿으로 사용.
- [ ] **Step 1: Implementation**
  - `app/agents/qa/knowledge.py`: `_REVERSED` 에 `"PART_OF": "contains"` 추가.
    기존 항목들과 같은 위치, 같은 딕셔너리 안에 `LEADS_TO` 아래 한 줄로.
  - `_REVERSED` 위쪽 주석(`LEADS_TO` 설명이 있는 그 블록)에는 손대지 않는다 —
    `PART_OF` 는 그 주석이 설명하는 사례(더 이상 못 쓰지만 과거에 쓰인 관계)와
    다르므로, 새 설명을 끼워 넣어 그 문단의 근거를 흐리지 않는다. 대신
    `KNOWLEDGE_RELATIONS` 옆 주석 쪽에 `PART_OF` 를 다루는 것으로 충분.
  - `KNOWLEDGE_RELATIONS` 옆 주석에 `PART_OF` 를 더하지 않는 이유를 추가: 구조
    관계이고 적재 경로(ingest path)가 만들며, agent 가 주장할 수 있는 관계가
    아니라는 점. 기존 주석(`LEADS_TO` 를 뺀 이유를 설명하는 문단)과 같은 톤과
    형식으로, 그 주석 블록 바로 아래에 이어 쓴다.
- [ ] **Step 2: Tests** — `tests/test_qa_knowledge_graph.py` 에
  `test_a_stored_route_still_reads_back_in_both_directions` 를 템플릿으로
  삼아 `PART_OF` 양방향 표기를 고정하는 테스트 추가:
  - `direction="OUT"` (hit=item) 렌더링에 `"part_of"` 가 나오고 `"contains"`
    는 나오지 않음을 확인.
  - `direction="IN"` (hit=document) 렌더링에 `"contains"` 가 나오고, 방향이
    거꾸로 읽히지 않는지(`"contained in"` 같은 표기가 없는지)까지 명시적으로
    확인 — 단어 자체를 고정해야, 방향은 다르지만 여전히 틀린 단어를 쓴 회귀를
    잡는다.
  - `KNOWLEDGE_RELATIONS` 에 `PART_OF` 가 없음을 확인하는 어서션 — 기존
    `set(KNOWLEDGE_RELATIONS) == {...}` 테스트가 이미 이를 담고 있으므로
    (그 시점의 튜플에 `PART_OF` 를 넣지 않는 한 자동으로 통과), 새 테스트를
    추가하기보다 그 기존 테스트가 여전히 통과하는지만 확인.
- [ ] **Step 3: Rollout / Rollback** — 순수 표기 변경, 마이그레이션 없음.
  되돌릴 경우 이 커밋만 revert 하면 됨.

## Validation
- **Commands to run:** `LANGSMITH_TRACING=false .venv/bin/python -m pytest`
- **Expected output:** 전체 통과, 단 `tests/test_config.py::test_settings_can_load_from_env_file`
  는 이 브랜치와 무관하게 이미 실패 중(셸의 실제 `OPENROUTER_API_KEY` 가 `.env`
  값을 덮어써서 발생) — pre-existing, 이번 변경으로 인한 것이 아님.

## Risks & Rollback
- **Risks:** 표기 문구를 잘못 고르면 방향이 다시 헷갈릴 수 있음 — 리뷰에서
  문구를 점검.
- **Rollback steps:** `git revert` 이 커밋.

## Open Questions
- (없음)

## Plan Review Notes

- Fast review (must-fix, 수용): 초안의 `_REVERSED["PART_OF"] = "contained in"`
  은 방향이 거꾸로다. `render_neighbour` 가 "hit 이(가) [label] 이웃" 으로
  읽는 것을 기존 네 관계로 검증하면, `direction == "IN"` 일 때 hit 은 관계의
  `to` 쪽이다. `PART_OF` 는 item→document 이므로 `to`=document 가 hit 이고,
  "문서가 [label] 항목" 이 "문서가 항목을 담고 있다"가 되려면 label 은
  `"contains"` 여야 한다. `"contained in"` 은 "문서가 항목에 담겨 있다"로
  읽혀 완전히 반대다. Context/Constraints 와 Step 1 을 고쳐 반영함.
- Fast review (should-fix, 수용): 테스트가 방향뿐 아니라 단어 자체
  (`"contains"`) 를 고정하도록 Step 2 를 구체화함.
- Medium review (PASS, should-fix 참고): 제안된 표기가 기존 라벨들과 어울리는
  과거분사형(`reached from`, `refined by`, `required by`, `replaced by`)과
  다르다는 지적 — fast review 로 정정된 `"contains"` 는 이 지적과 무관하게
  올바른 방향이므로 그대로 채택. `KNOWLEDGE_RELATIONS` 주석 위치는 그 옆에
  이어 쓰는 것으로 Step 1 에 명시함.
- Under-scoping 없음: dict 항목, 주석, 양방향 테스트가 AC 를 모두 덮음.

## Pair Review Notes

VERDICT: PASS. `render_neighbour` 의 `direction`/`_REVERSED` 로직을 기존 네
관계로 독립 검증했고, `"contains"` 가 `PART_OF`(item→document) 의 `IN` 방향
표기로 맞다고 확인함. `KNOWLEDGE_RELATIONS` 는 그대로 두었고, 새 tool 이나
검색 필터를 만들지 않아 범위도 지켜짐. 새 테스트가 방향뿐 아니라 반대쪽
단어가 안 섞였는지까지 어서션하는 것도 긍정적으로 평가됨.

should-fix 2건, 둘 다 반영:

1. 주석 언어 — `coding-style.md` 의 "Write comments in Korean" 은 새로 쓰는
   문장에 무조건 적용되고, "기존 영어 주석은 결함이 아니다" 예외는 기존
   텍스트를 다시 번역하지 말라는 것이지 새 문장의 언어를 정하는 규칙이 아니라는
   지적. `KNOWLEDGE_RELATIONS` 옆에 새로 넣은 문단을 한국어로 다시 씀
   (`app/agents/qa/knowledge.py` 137-143번째 줄 부근).
2. 용어 — 초안의 "기획서 node" 가 ARTEL-748 migration 이 실제로 쓰는 이름
   "문서 node" 와 다르다는 지적. "문서 node" 로 고침.

두 항목 모두 반영 후 `tests/test_qa_knowledge_graph.py tests/test_qa_knowledge.py`
110 passed 로 재확인.
