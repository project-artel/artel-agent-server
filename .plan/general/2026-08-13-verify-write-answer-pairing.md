# 2026-08-13 — 지식 쓰기 답의 짝을 검증한다

- Date: 2026-08-13
- Jira: ARTEL-367
- Status: Implemented

## Goal

쓰기 응답의 `type` echo가 보낸 요청과 맞는지 확인하고, 어긋나면 그 답을 쓰지 않는다.

## 범위가 줄었다

원래 이 이슈는 **무응답 쓰기의 재시도**가 절반이었다. ARTEL-364(지식 쓰기 멱등)가 필요 없다는
판단으로 닫히면서 그 절반이 함께 빠졌다 — 제약 첫 줄이 "364 없이 재시도를 켜지 마라, 멱등 없는
재시도는 중복 생성기다"였고 그 전제가 사라졌기 때문이다. Jira 코멘트에 기록했다.

남은 것이 짝 검증이고, 그것은 재시도와 무관하게 단독으로 값어치가 있다.

## Non-goals

- 무응답 재시도 일체. 무응답은 계속 '모름'으로 남고 `UNCONFIRMED_WRITE` 문구도 그대로다.
- Orchestration의 멱등 — ARTEL-364(닫힘, 브랜치에 구현 남아 있음).
- 툴 설명 문구 — ARTEL-368.

## 결정

### 결정 1 — 대기 항목이 "무엇을 물었나"를 함께 진다

`_pending`이 future만 들고 있어서 답을 요청과 대조할 수 없었다. `_PendingRequest(waiter,
request_type)`으로 바꿔 비교 대상을 손에 쥔다.

### 결정 2 — 어긋난 답은 **버린다.** 실패로 만들지 않는다

correlation만으로는 이것을 못 잡는다는 것이 요점이다. 어긋난 답에 실린 id도 **진짜 id**라서,
믿으면 엉뚱한 항목이 `knowledge_seen`에 들어가고 그 뒤의 수정이 전부 그 행으로 간다 — 조용히.

그렇다고 실패로 올리지는 않는다. 짝이 어긋난 것은 저쪽의 프로토콜 결함이지 런이 대응할 수 있는
일이 아니고, 실패로 말하면 모델이 **저장됐을 수도 있는** 사실을 다시 쓴다. 버리면 툴이 타임아웃해
'확인 못 받음'으로 떨어지는데, 그것이 실제 상황에 맞는 말이다.

### 결정 3 — 검증은 쓰기 응답에만

검색·확장 응답에는 echo 필드가 없다. 없는 것을 검사할 수는 없고, 만들자고 그쪽 계약을 넓히는 것은
이 이슈의 범위가 아니다.

## Approach (Checklist)

- [x] `_PendingRequest` 도입, `_pending`·`on_cancel`·`_resolve`를 그것에 맞춤
- [x] `on_knowledge_write_result`가 echo를 대조하고 어긋나면 경고 로그 후 버림
- [x] 테스트: 어긋난 답이 `knowledge_seen`에 들어가지 않고 '모름'으로 떨어지는지

## Validation

- `../../.venv/bin/python -m pytest tests/test_qa_channel.py tests/test_qa_knowledge.py -q` — 84 passed
- `../../.venv/bin/python -m pytest -q` — **512 passed**

기존 `test_qa_channel.py`가 수정 없이 통과한다 — 대기 구조를 다시 건드렸으므로 그것이 검색·확장
경로가 그대로라는 증거다.

## Risks & Rollback

- **Risks:** 어긋난 답을 버리면 툴이 타임아웃을 다 기다린다(쓰기 기준 5초). 실제로 어긋나는 일이
  없으므로 비용이 0이고, 생기는 날에는 그 5초가 문제의 가장 작은 부분이다.
- **Rollback steps:** `git revert`.

## Open Questions

- 없음.
