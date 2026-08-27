# 2026-08-27 — record_knowledge 에 앵커를 싣고 검색 결과에 보인다

- Date: 2026-08-27
- Jira: ARTEL-592
- Status: Implemented (테스트 통과, PR 미개설)

## Goal

한 화면에서만 참인 지식이 어느 화면 것인지 말할 수 있게 한다. `KNOWLEDGE_CREATE`
프레임에 `scene_name` 과 `screen_id` 를 선택 필드로 붙이고, `record_knowledge`
도구가 두 인자를 받아 그대로 실어 보낸다. 검색 결과에는 앵커가 붙은 히트가 한 줄로
그 사실을 말한다.

ARTEL-590 이 이미 기준을 산문으로 써 두었다 — "여기서만 참이면 어디인지 말하고,
어디서나 참이면 화면을 말하지 않는다". 그때는 인자가 없어서 산문이었고, 이번에
인자를 만들고 설명이 그 인자를 이름으로 부르게 한다.

## Non-goals

- 앵커를 나중에 고치는 도구는 만들지 않는다 (`KNOWLEDGE_UPDATE` 는 앵커를 보지 않는다).
- orchestration 서버와 프런트엔드는 건드리지 않는다 (ARTEL-591 이 받는 쪽이다).
- 프롬프트 본문은 손대지 않는다. v13 은 이 이슈의 것이 아니다.
- 검색 요청에 `scene_name` 필터를 붙이지 않는다. orchestration 이 그 필터를 받지만
  (ARTEL-591), agent 쪽 AC 에 없다.

## Context / Constraints

- **현재 씬을 자동으로 채우지 않는다.** 이것이 이 이슈에서 가장 중요한 규칙이다.
  런이 서 있던 화면으로 자동으로 채우면 게임 전체 규칙이 그 화면 하나에 갇히고,
  다음 화면에 선 런은 그 규칙을 영영 못 찾는다. 도구가 인자를 받고, agent 가
  명시할 때만 실린다.
- **선택 필드다.** `KnowledgeSearchPayload.step` 이 같은 이유로 선택이다 — 이 필드를
  모르는 orchestration 은 모르는 payload 필드를 무시하므로 배포 순서가 자유롭다.
  `outbound_envelope` 는 `model_dump()` 를 `exclude_none` 없이 쓰므로 앵커 없는
  쓰기는 `"scene_name": null, "screen_id": null` 로 나간다. `step` 이 이미 그렇게
  나가고 있고, orchestration 은 null 을 "없음" 으로 읽는다.
- **`screen_id` 없이 `scene_name` 은 되지만 그 반대는 안 된다.** ARTEL-591 이
  `sceneName` 없는 `screenId` 를 거절한다 — 화면은 씬 안에 살고, 씬을 모르는 화면
  앵커는 나중에 어느 씬의 화면이었는지 되짚을 수 없다. `tag` 와 빈 `summary` 를
  이미 이 쪽에서 먼저 거절하듯, 이것도 프레임을 보내기 전에 거절한다.
- **검색 히트의 앵커는 `anchors` 배열이다.** ARTEL-591 의 `KnowledgeSearchHit` 이
  `anchors: List<KnowledgeAnchorView>` 를 순수 추가 필드로 싣는다. 한 지식이 여러
  화면에 묶일 수 있어서 배열이다. `KnowledgeAnchorView` 는 `scene_name` (필수) 과
  `screen_id` (문자열, null 허용) 다 — 다른 id 계열과 같이 문자열로 나온다.
- 히트 렌더의 앵커 줄은 이웃 블록(`<<neighbours of ...>>`) **앞**에 둔다.
  `fold_stale_knowledge` 가 그 블록만 정확히 갈아치우므로, 앞에 두면 접기와 무관하다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `app/qa/envelope.py`, `app/agents/qa/knowledge.py`,
      `app/agents/qa/tools.py`, `tests/test_qa_knowledge.py`,
      `tests/test_qa_knowledge_fold.py` 를 읽는다. ARTEL-591 의 DTO 로 와이어 모양을
      맞춘다.
- [x] **Step 1: `app/qa/envelope.py`**
  - `KnowledgeCreatePayload` 에 `scene_name: str | None = None`,
    `screen_id: str | None = None` 과 앵커가 무엇이고 왜 선택인지 말하는 주석
  - `KnowledgeAnchor` 모델 신설 (`scene_name`, `screen_id`), 두 필드 모두 기본값 —
    `KnowledgeSearchHit` 의 규칙(검증 실패한 히트 하나가 답 전체를 무너뜨린다)을 따른다
  - `KnowledgeSearchHit.anchors: list[KnowledgeAnchor] = Field(default_factory=list)`
- [x] **Step 2: `app/agents/qa/tools.py`**
  - `record_knowledge` 에 `scene_name: str | None = None`,
    `screen_id: str | None = None`
  - 빈 문자열은 씻어서 None 으로, `screen_id` 만 온 경우는 거절
  - payload 에 그대로 전달. 런의 현재 씬은 읽지 않는다
- [x] **Step 3: `app/agents/qa/knowledge.py`**
  - `RECORD_KNOWLEDGE_DESCRIPTION` 의 ARTEL-590 문단을 늘려 인자를 이름으로 부른다.
    기준은 다시 말하지 않는다
  - `render_hit` 에 앵커 한 줄. 앵커 없으면 오늘과 완전히 같은 출력
  - `render_anchors` 헬퍼
- [x] **Step 4: Tests** — `tests/test_qa_knowledge.py`
  - 앵커 없는 쓰기의 프레임 (기존 테스트 확장)
  - 두 필드 다 실은 쓰기
  - `scene_name` 만 실은 쓰기
  - `screen_id` 만 온 쓰기는 프레임이 안 나간다
  - 앵커 있는 히트 / 없는 히트 렌더
- [x] **Step 5: Validation** — `env -u OPENROUTER_API_KEY uv run --extra dev pytest`
- [x] **Step 6: Review + commit**

## Risks

- ARTEL-591 이 아직 비행 중이다. `screen_id` 는 양쪽 다 문자열이라 (Kotlin `String?`)
  강제 변환에 기대지 않는다. 이 파일의 다른 모든 id 와 같은 규칙이다.
- `anchors` 배열 모양은 ARTEL-591 의 미머지 작업 트리에서 읽은 것이다. 머지 전까지
  확정이 아니다. 순수 추가 필드라 틀려도 히트가 앵커 줄을 잃을 뿐 답이 무너지지는
  않는다.
