# 2026-08-05 — QA Agent에 지식 수정 도구(update_knowledge)를 추가한다

- Date: 2026-08-05
- Jira: ARTEL-257
- Status: Implemented

## Goal

QA 에이전트가 기존 지식 항목을 **한 번의 호출로** 고칠 수 있게 한다. 그 호출은
Orchestration이 이미 처리하는 `KNOWLEDGE_UPDATE` 프레임으로 나가고, 그 결과
`updated_by_qa_try_id`가 채워져 이력에서 "수리"와 "폐기"가 갈린다.

## Non-goals

- Orchestration 수정. UPDATE 경로는 이미 end-to-end로 산다.
- 인용 기능(`report_step`의 `used_knowledge_ids`), `KnowledgeSearchPayload`의 `step`.
- `knowledge_event` / `knowledge_usage` / 집계 view, `knowledge.scope_id` 격리(ARTEL-256).
- 검색·임베딩 동작 변경. 되살리기 도구.

## Context / Constraints

**ARTEL-189이 이 도구를 뺀 이유는 "도구 표면 축소"였다.** 안전성이나 데이터 무결성이
아니다. 그 이슈 본문이 대가 두 가지(원자성 상실, 계보 단절)를 스스로 적어 두었고,
ARTEL-239가 실행 설정 축 비교를 시작하면서 계보 단절이 실제 비용이 되었다. 근거가
뒤집혔으므로 결정을 되돌린다.

되돌리는 것이 정상 경로의 복잡도를 줄인다: 도구 하나가 2단 흐름과 그 안전장치들(강한
지시문, 예산 면제, 미완 경고)을 대체한다. 다만 모델은 여전히 삭제+생성을 고를 수
있으므로 그 장치들 자체는 안전망으로 남긴다.

와이어 제약:
- 지식 쓰기는 **응답이 없다.** 성공은 침묵, 거부는 ORCHE_INTERNAL 행. 이쪽에서 응답을
  기다리는 코드를 만들면 그 도구는 타임아웃까지 매달린다.
- `KnowledgeMutationRequest`가 UPDATE에서 받는 것: `knowledge_id`(문자열 숫자),
  `tag`/`summary`/`description`. null이면 그 필드는 그대로 두고, 셋 다 null이면 거절,
  빈 문자열도 거절.

이 브랜치는 `origin/develop`에서 딴다. ARTEL-238(`run_config`)·ARTEL-237(압축)·
ARTEL-242가 그 사이 머지되어, 예산 상수는 `app/agents/qa/arch.py`로 옮겨 갔고
프롬프트 최신 버전은 v6이다.

## Approach (Checklist)

- [x] **Step 0: Recon**
  - ARTEL-189에서 제외 사유 확인 → 툴 단순화. 진행 가능.
  - `QaAgentInboundRouter.KNOWLEDGE_MUTATION_TYPES`, `KnowledgeService.updateFromQaTry`,
    `KnowledgeMutationRequest` 필드 계약 확인.
- [x] **Step 1: 와이어** — `app/qa/envelope.py`
  - `MessageType.KNOWLEDGE_UPDATE` 추가, 부재를 설명하던 주석 교체.
  - `KnowledgeUpdatePayload`: `knowledge_id: str` 필수 + `tag`/`summary`/`description`
    optional. Create/Delete와 별도 모델(기존 주석 논리 그대로 — 셋 중 id와 본문을 둘 다
    갖는 것은 이것뿐이다).
- [x] **Step 2: 도구** — `app/agents/qa/knowledge.py`, `app/agents/qa/tools.py`
  - `UPDATE_KNOWLEDGE_DESCRIPTION` 신설. forget과의 경계, 부분 수정 의미, id 규칙.
  - `update_knowledge(step, thought, knowledge_id, tag=None, summary=None, description=None)`.
  - id 가드는 `forget_knowledge`와 동일(`knowledge_seen` 멤버십).
  - 예산: `max_records_per_run`을 record와 **공유**한다. 근거는 아래 결정 항목.
  - 성공 시 `knowledge_seen`을 새 요약으로 갱신. `summary`를 안 보냈으면 그대로 둔다.
  - 실패는 `record_knowledge`와 같은 처리 — `QaCancelled`는 재던지고, 나머지는
    "아무것도 바뀌지 않았다"를 명시하며 런은 계속한다.
- [x] **Step 3: 2단 수리 장치 정리**
  - 남긴다: `knowledge_deleted_unreplaced`, `render_missing_knowledge_warning`,
    `record_knowledge`의 outstanding 예산 면제, `QaArchSpec.forgets_need_records`.
  - 바꾼다: `forget_knowledge`의 설명과 결과 문자열이 "고치려면 `update_knowledge`"를
    먼저 가리키고, 삭제+생성 지시는 그 뒤에 안전망으로 남는다. `record_knowledge`의
    설명은 이미 있는 항목을 고치는 경우를 update로 보낸다.
- [x] **Step 4: 프롬프트 v7**
  - `app/prompts/qa_run/v7/{system,vision_directive}.md`. 본문은 v6과 **동일** —
    지식 도구 사용 지침은 ARTEL-192에 따라 툴 설명이 단일 출처이고, 시스템 프롬프트에는
    지식 도구 얘기가 한 줄도 없다. 버전은 도구 집합이 바뀐 런을 가르기 위한 표식이며,
    그 사실을 note에 적는다.

### 결정: 예산을 record와 공유한다

`update_knowledge`는 `max_records_per_run`을 `record_knowledge`와 하나의 예산으로 쓴다
(`QaRunState.knowledge_writes_attempted`).

- 둘 다 "지식창고에 내용을 얹는 쓰기"라 같은 실패 모드(런이 지식 정리에 스텝을 태우고
  판정을 못 함)를 만든다. 별도 예산은 그 총량을 소리 없이 두 배로 늘린다.
- `ResolvedArch.tool_call_limit`이 그대로 유지된다 — 새 도구가 루프 예산을 늘리지 않는다.
- `QaArchSpec`에 축을 하나 더 열지 않아도 된다. 수정만 따로 조이고 싶어질 근거가 아직 없다.
- "수리가 예산에 막혀 반쪽으로 끝나면 안 된다"는 제약은 양쪽에서 지켜진다. `update`는
  원자적이라 거부돼도 항목이 그대로 남고, 삭제가 이미 나간 경우의 재기록은 기존
  outstanding 면제가 계속 막아 준다(그 면제도 합산 총량을 읽는다).

## Validation

- **Commands run:**
  - `.venv/bin/python -m pytest tests/test_qa_knowledge.py tests/test_qa_tools.py -q` → 90 passed
  - `.venv/bin/python -m pytest -q` → 420 passed
  - `resolve_run_config()` 직접 호출 → `prompt_version=v7`, `tools`에 `update_knowledge`
    포함, `agent_fingerprint` 변경 확인
- **새 테스트가 고정하는 것:**
  - `knowledge_seen`에 없는 id 거부 / 이 런에서 삭제한 id도 거부
  - 전송 실패 시 런 유지 + "아무것도 바뀌지 않았다" + 항목이 그대로임
  - 예산이 record와 합산으로 소진 / 합산이어도 재기록 면제는 살아 있음
  - 수정 후 `knowledge_seen` 갱신 → 이후 forget 라벨이 새 요약을 쓴다
  - `summary`를 안 보낸 수정은 라벨을 건드리지 않는다
  - forget → record 2단 경로 회귀(런너 레벨)
  - 프레임이 `KnowledgeMutationRequest` 모양과 일치(생략 필드는 null로 나간다)

## Risks & Rollback

- **Risks:**
  - 도구가 하나 늘어 모델이 forget과 update를 혼동할 수 있다. 완화는 두 설명의 경계
    문장과 결과 문자열. 툴 설명 총량은 4,664자 → 6,250자로 늘었고, forget에서 걷어낸
    분량은 그보다 작다 — 순증이며 PR에 그대로 적는다.
  - 예산 공유로 "기록 5회"가 "기록+수정 합쳐 5회"가 된다. 의도된 변경이며 두 도구 설명이
    그 사실을 말한다.
- **Rollback steps:** `git revert`. 와이어 타입 추가는 Orchestration이 이미 받는
  타입이라 되돌려도 계약이 깨지지 않는다. 프롬프트 v7은 남아도 무해하다.

## Open Questions

- 없음.
