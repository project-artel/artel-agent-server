# 2026-08-27 — qa_run v12 — 화면 지도 절을 빼고 앵커 기준을 넣는다

- Date: 2026-08-27
- Jira: ARTEL-590
- Status: Draft

## Goal

qa_run 프롬프트와 지식 도구 설명에서 화면 지도를 짓는 지시를 걷어내고, 그 자리에
"화면·씬에 지식을 묶는 기준"을 넣는다. 화면 목록과 화면 사이 경로는 orchestration
의 `content_map` 스키마(`screen`, `screen_capability`, `screen_transition`,
`scene_edge`)가 이미 소유하므로, 지식 베이스가 같은 지도를 한 벌 더 짓는 것을
멈춘다. 지식 베이스에 남는 것은 다른 곳에 칸이 없는 것 — 게임 관례, 규칙, 목표 —
그리고 한 화면에서만 참인 사실이다.

## Non-goals

- 앵커 인자(scene name, screen id) 자체는 구현하지 않는다. 별도 이슈다
  (이 레포는 ARTEL-592, orchestration 은 ARTEL-591). 이번 프롬프트 절은 어떤
  인자를 넘기는지가 아니라 **무엇이 지식 베이스에 속하는지**를 말한다.
- 이미 저장된 지식 항목이나 `LEADS_TO` 간선을 지우거나 마이그레이션하지 않는다.
- v11 이하는 잠긴 릴리스다. 손대지 않는다.
- 기본 프롬프트 버전을 설정으로 바꾸지 않는다.

## Context / Constraints

- `app/prompts/loader.py` 의 `resolve_version` 은 명시 인자 → `qa_prompt_version`
  설정 → 최신 디렉터리 순으로 고른다. `.env.example` 의 `QA_PROMPT_VERSION` 은 빈
  문자열이고 빈 값은 "미설정"으로 읽히므로, `v12` 디렉터리를 만드는 순간 고정하지
  않은 모든 런이 v12 로 옮겨간다.
- `app/prompts/lock.py` 가 배포된 프롬프트 본문의 해시를 커밋된 JSON 과 비교한다.
  버전을 추가한 뒤 `python -m app.prompts.lock --write` 로 재생성해야 한다.
- ARTEL-192: 도구 설명이 사용 정책의 단일 출처다. 프롬프트만 고치면 agent 는
  `RECORD_KNOWLEDGE_DESCRIPTION` 을 보고 여전히 화면당 항목을 만든다.
- `_REVERSED` 는 쓰기 어휘가 아니라 **읽기** 표다. 기존 `LEADS_TO` 간선이 검색·확장
  결과에 계속 렌더링되어야 하므로 남긴다.

## Approach (Checklist)

- [ ] **Step 0: Recon** — `app/prompts/qa_run/v11/`, `app/prompts/lock.py`,
      `app/prompts/loader.py`, `app/agents/qa/knowledge.py`,
      `app/agents/qa/tools.py`, `tests/test_qa_prompt_version.py`,
      `tests/test_qa_knowledge_graph.py` 를 읽는다.
- [ ] **Step 1: Implementation**
  - `app/prompts/qa_run/v12/system.md` — v11 복사 후
    - `### The screen map` 절 전체 삭제
    - `### Structuring the rest of what you know` 첫 문장을 화면 지도에 기대지 않게 다시 쓴다
    - `### Removing a link` 의 `LEADS_TO` 특정 부분 제거, 브레이크는 지식 삭제가
      아니라 보고로 간다는 요지는 남긴다
    - `### What belongs to a screen` 절 신설 — 이 화면에서만 참이면 묶고, 게임
      어디서나 참이면 묶지 않는다. 화면 목록과 경로는 더 이상 지식 베이스 소재가
      아니다
  - `app/prompts/qa_run/v12/vision_directive.md` — v11 그대로, note 만 갱신
  - `app/agents/qa/knowledge.py`
    - `RECORD_KNOWLEDGE_DESCRIPTION` 의 화면 문단을 앵커 기준으로 교체
    - `LINK_KNOWLEDGE_DESCRIPTION` / `UNLINK_KNOWLEDGE_DESCRIPTION` 에서
      `LEADS_TO` 항목과 문단 제거
    - `KNOWLEDGE_RELATIONS` 에서 `LEADS_TO` 제거
    - `_REVERSED` 는 `LEADS_TO` 유지 + 왜 남는지 주석
  - `app/agents/qa/tools.py` — `record_knowledge` 의 죽은 `ui_tag` 포맷 인자 제거,
    `link_knowledge` 의 `note` 거절문에서 `LEADS_TO` 문구 제거
  - `app/prompts/prompts-lock.json` 재생성
- [ ] **Step 2: Tests**
  - `tests/test_qa_prompt_version.py` — 기본 버전 v12, v12 가 화면 지도 절을 잃고
    앵커 기준을 얻었다는 검사, 역할 동일성
  - `tests/test_qa_knowledge_graph.py` — `LEADS_TO` 는 쓰기 어휘에서 빠졌고
    (link·unlink 가 거절한다), 저장된 `LEADS_TO` 간선은 여전히 렌더링된다
- [ ] **Step 3: Rollout / Rollback** — 플래그 없음. 롤백은 `QA_PROMPT_VERSION=v11`
      환경변수 고정(프롬프트만) 또는 커밋 revert(도구 어휘까지).

## Validation

- **Commands to run:** `uv run pytest`
- **Expected output:** 전체 통과.

## Risks & Rollback

- **Risks:**
  - `KNOWLEDGE_RELATIONS` 에서 `LEADS_TO` 가 빠지면 `unlink_knowledge` 도 기존
    `LEADS_TO` 간선을 지울 수 없게 된다. 이번 이슈가 "지도를 지우지 말라"고 말하는
    방향과 같으므로 의도된 결과로 둔다. 나중에 잘못된 경로를 지워야 하면 읽기
    어휘와 쓰기 어휘를 분리해야 한다.
  - `EXPAND_KNOWLEDGE_DESCRIPTION` 이 `{relations}` 로 쓰기 어휘를 출력하므로,
    확장 결과에 나온 `LEADS_TO` 가 그 목록에 없다. 모델이 "누가 주장한 관계"로
    읽지 못할 여지가 조금 있으나, 설명은 `SIMILAR` 와의 대비를 말하는 것이고
    `LEADS_TO` 는 `↳` 로 렌더링되므로 구분은 유지된다.
  - v12 가 자동으로 기본이 된다. 배포에서 되돌리려면 `QA_PROMPT_VERSION` 을 명시로
    고정해야 한다.
- **Rollback steps:** `QA_PROMPT_VERSION=v11`, 또는 커밋 revert.

## Open Questions

- 없음. 앵커 인자는 ARTEL-592 가 가져간다.
