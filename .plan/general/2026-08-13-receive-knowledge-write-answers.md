# 2026-08-13 — 지식 쓰기의 거절과 id를 툴이 받는다

- Date: 2026-08-13
- Jira: ARTEL-332
- Status: Implemented

## Goal

지식 쓰기 다섯 툴이 Orchestration의 답을 받는다. 거절은 거절로 모델에게 말하고,
`record_knowledge`는 만들어진 항목의 id를 받아 같은 런이 검색 없이 그것을 고칠 수 있게 한다.

짝인 ARTEL-331이 Orchestration 쪽 계약을 이미 확정했다. 이 이슈는 그 계약을 소비할 뿐이고,
계약 자체를 다시 정하지 않는다.

## 받게 되는 계약 (ARTEL-331에서 확정)

- 성공: `KNOWLEDGE_WRITE_RESULT`, 요청 messageId를 correlation으로.
  payload는 `{"type": "<요청 타입>", "knowledge_id" | "edge_id": "<문자열 id>"}`
- 거절: 요청의 correlation을 문 `ERROR`. 검색·확장이 이미 쓰는 것과 같다.
- 배치 인입 `KNOWLEDGE`는 답하지 않는다.
- 게이트(`knowledge_mode`) 거부도 ERROR로 답한다.
- 답할 세션이 없으면 Orchestration은 쓰기를 수행하고 답만 보내지 않는다.
- 라우팅 전에 버려지는 프레임(모르는 런 등)은 여전히 답이 없다.

마지막 두 줄이 이 이슈의 **무응답을 '모름'으로 다루라**는 제약의 근거다. 무응답은 "저장 안 됨"이
아니라 "저장됐는지 모름"이다.

## Non-goals

- Orchestration의 RESULT 발신 — ARTEL-331 (PR #116).
- ARTEL-317의 인덱싱 지연. 다른 런이 이 항목을 검색으로 찾는 문제는 그대로다.
- `ISSUE` 프레임.
- 프롬프트 변경. 툴 **설명**(`*_DESCRIPTION`)은 건드리지 않는다 — 아래 결정 3.

## 결정

### 결정 1 — 대기 상태를 messageId 키의 맵 하나로 모은다.

지금은 요청 종류마다 필드 두 개다(`_knowledge_waiter`/`_pending_knowledge_id`,
`_expand_waiter`/`_pending_expand_id`). 쓰기까지 더하면 여섯 개가 되고, 그 필드들이 존재하는
이유였던 "미결 프레임 둘이 서로의 future를 푸는 버그"는 correlation 하나로 이미 막을 수 있다.

`self._pending: dict[str, asyncio.Future]` 하나로 바꾸고, 보내기·기다리기·정리를 `_request`
하나에 모은다. 얻는 것:

- 교차 해소 가능성이 구조적으로 사라진다. 키가 messageId이므로 남의 답이 내 future에 닿을 수 없다.
- `on_error`가 분기 세 개 대신 조회 한 번이 된다.
- **기존 버그 하나가 함께 닫힌다**: `on_cancel`이 검색 waiter만 취소하고 확장 waiter는 두고 간다.
  맵이면 전부 쓸어 취소한다. 지금은 운영자가 런을 끝내도 확장에 걸린 툴이 20초를 더 산다.

액션 waiter(`_action_waiter`)는 **건드리지 않는다.** 타임아웃과 취소 의미가 다르고, 이 이슈가
요구하는 범위 밖이다. 같이 옮기면 diff가 지식 경로 밖으로 번진다.

### 결정 2 — `KnowledgeSearchFailed` → `KnowledgeRequestFailed`.

쓰기 거절도 같은 타입으로 돌아온다. "Search"가 이름에 남으면 거짓이 된다. 기계적 이름 변경이고
동작은 그대로다(ARTEL-331이 `failSearch` → `answerWithError`로 한 것과 같은 판단).

### 결정 3 — 툴 **설명**은 바꾸지 않는다. 툴 **결과 문구**만 바꾼다.

`RECORD_KNOWLEDGE_DESCRIPTION` 등은 모델이 읽는 프롬프트다. 바꾸면 `prompt_version`을 올릴지
판단해야 하고, 이 이슈의 목적은 "결과를 정직하게 말하는 것"이지 "쓰는 법을 다시 가르치는 것"이
아니다. 설명이 지금 약속하는 것("Nothing answers a knowledge write")이 사실과 어긋나게 되는
지점은 있으나, 그것을 고치는 것은 결과 문구가 새 사실을 말하기 시작한 **뒤**가 맞다. 후속으로
남긴다.

### 결정 4 — 무응답 문구는 한 곳에서 온다.

세 가지 결과(확인됨 / 거절됨 / 모름) 중 **모름**의 문구만 공유 상수로 둔다. 나머지 두 개는 툴마다
말이 다르므로(기록됨·고쳐짐·지워짐·연결됨·거둬짐) 각자 쓴다 — 공용 렌더러로 묶으면 명사만 갈아
끼우는 틀이 되어 문장이 어색해지고, 그 틀이 곧 `utils` 서랍이 된다.

'모름'만 공유하는 이유는 그 문장이 **부하를 지는** 문장이기 때문이다. "실패했다"로 흘러가는 순간
모델이 같은 사실을 다시 쓰고, 없애려던 중복이 새 경로로 생긴다. 한 곳에 두면 표현이 갈라지지
않는다.

### 결정 5 — 쓰기 타임아웃 5초.

검색의 20초를 쓰지 않는다. 저쪽 일은 LLM 왕복이 아니라 DB 쓰기 한 번이다. 짧게 잡아야 응답을
보내지 않는 구버전 Orchestration과 붙었을 때 쓰기마다 상한을 다 태우는 사태를 면한다.

쓰기 예산은 런당 한 자릿수라 최악이 5초 × 몇 번이고, 그마저도 결과가 '모름'이지 실패가 아니다.

## Approach (Checklist)

- [x] **Step 0: Recon** — `channel.py`(waiter 구조, `on_error`, `on_cancel`), `service.py.deliver`,
      `tools.py`의 다섯 쓰기 툴, `envelope.py`의 타입 목록
- [x] **Step 1: `envelope.py`** — `KNOWLEDGE_WRITE_RESULT` 타입과 `KnowledgeWriteResultPayload`.
      쓰기 타입들의 "ONE-WAY" 주석을 사실에 맞게 고친다
- [x] **Step 2: `channel.py`** — `_pending` 맵과 `_request`, `write_knowledge`가 답을 돌려주게,
      `on_knowledge_write_result`, `on_error`/`on_cancel` 정리, `KnowledgeRequestFailed` 개명
- [x] **Step 3: `service.py`** — `deliver`에 분기 하나
- [x] **Step 4: `tools.py`** — 다섯 툴이 세 결과를 갈라 말한다. `record_knowledge`가 받은 id를
      `knowledge_seen`에 넣는다(`knowledge_glimpsed`가 아니다 — 자기가 쓴 본문은 읽은 것이다)
- [x] **Step 5: 테스트** — `tests/test_qa_channel.py`, `tests/test_qa_knowledge*.py`

## Validation

- **Commands to run:**
  - `python -m pytest tests/test_qa_channel.py tests/test_qa_knowledge.py tests/test_qa_knowledge_graph.py -q`
  - `python -m pytest -q` (계약 공유 범위)
- **Expected output:** 전부 통과. 특히 응답을 보내지 않는 Orchestration을 흉내 낸 케이스에서 런이
  끝까지 돌아야 한다.

## Risks & Rollback

- **Risks:**
  - waiter 구조 변경이 검색·확장 경로까지 건드린다. 이 이슈에서 가장 큰 위험이고, 기존
    `test_qa_channel.py`가 수정 없이 통과하는 것이 그 방어다.
  - 쓰기가 이제 await한다 — 런 시간이 늘어난다. 상한 5초 × 쓰기 횟수. 구버전 Orchestration에서
    최악이다.
  - 툴 설명과 결과 문구가 한 릴리스 동안 어긋난다(결정 3). 설명은 "아무것도 답하지 않는다"라고
    말하는데 결과는 확인을 말한다. 모델이 읽는 것은 결과이므로 해는 작지만, 후속으로 닫아야 한다.
- **Rollback steps:** `git revert`.

## 구현 결과

Status: Implemented. Approach의 다섯 단계 모두 계획대로 갔고, 검토에서 두 가지가 더 나왔다.

- **stale 주석 여섯 곳.** "쓰기는 ONE-WAY라 거절이 모델에게 안 보인다"가 코드 곳곳의 판단 근거로
  적혀 있었다(`tools.py` 넷, `envelope.py` 하나, `knowledge.py` 하나). 이 변경으로 전부 거짓이
  된다. 로컬 검증을 **왜 남기는지**도 바뀐다 — "그것 말고는 막을 것이 없어서"가 아니라 "왕복
  한 번을 아껴서"다. 그렇게 고쳤다.
- **테스트 스위트가 4배 느려졌다.** 쓰기가 await하기 시작하자 대역이 답하지 않는 기존 테스트마다
  5초가 붙어 지식 스위트가 46초 → 232초가 됐다. `write_timeout`을 `action_timeout`과 같이
  주입 가능하게 하고 테스트 하네스가 0.05를 넘긴다. 실환경 기본값은 그대로다.

`test_a_write_does_not_wait_for_an_answer_that_never_comes`를 지웠다. 그 테스트가 고정하던 것이
바로 이 이슈가 바꾸는 계약이다. 대신 다섯 개를 넣었다 — id 왕복, 거절 전달, 거절된 삭제가
outstanding을 남기지 않음, 무응답='모름', 늦은 답이 다음 요청을 풀지 않음.

### 계획 대비 남긴 것

툴 설명(`*_DESCRIPTION`)에 "Nothing answers a knowledge write, so send each fact once"가 세 곳
남아 있다(결정 3). 전제는 이제 거짓이지만 **지시("한 번만 보내라")는 여전히 맞고**, 그것이 이
이슈가 모델에게 원하는 행동이다. 프롬프트를 건드리면 `prompt_version` 판단이 따라오므로 후속으로
남긴다.

### 검증

```
../../.venv/bin/python -m pytest -q     511 passed (58s)
```

지식·채널·툴 스위트만: 147 passed (46s).

## Open Questions

- 없음.
