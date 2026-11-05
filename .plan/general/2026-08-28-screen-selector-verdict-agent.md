# 2026-08-28 — 화면 제안을 판정하는 agent 를 따로 띄운다

- Date: 2026-08-28
- Jira: ARTEL-656 (`SCREEN_SETTLED` 배선은 ARTEL-668)
- Status: Implemented

## Goal

`SCREEN_SELECTOR_PROPOSAL` 이 오면 QA agent 와 **분리된** 짧게 사는 agent 가 그것을 읽고
`SCREEN_SELECTOR_VERDICT` 로 답한다. 답은 개별 화면 판정이 아니라 whitelist 항목 배열이다.

`SCREEN_SETTLED` 도 함께 붙인다 (ARTEL-668). 같은 파일 셋을 건드리고, 두 작업이 나뉘면
서로의 diff 를 밟는다.

## Non-goals

- whitelist 를 저장하고 적용하는 것 — ARTEL-654 (orchestration)
- 제안을 만들어 보내고 기존 행을 합치는 것 — ARTEL-655 (orchestration)
- QA agent 가 직접 목록을 고치는 tool — ARTEL-657 (이 브랜치의 base)

## Context / Constraints

계약은 orchestration 의 `docs/screen-selector-frames.md` 와
`contentmap/observe/ScreenSelectorFrames.kt` (PR #212, ARTEL-655) 다. 프레임을 새로 짓지
않는다.

- `SCREEN_SELECTOR_VERDICT` payload: `{ proposal_id, entries: [...], note }`,
  봉투의 `correlationId` 는 제안의 `messageId`
- `match` 는 `selector` · `path` · `subtree` 셋뿐. `pattern` 은 정확 문자열, 512자 이하,
  정규식 금지
- 기본값이 무시다. 답을 안 내는 것과 `screen_defining=false` 가 같은 결과다
- 모델이 형식을 어기면 지어내지 않고 항목 없는 실패로 답한다
- **프롬프트에 특정 게임의 관례를 적지 않는다.** 이 SDK 는 Unity 게임 일반에 붙는다
- **판정 실패·지연이 QA 런에 닿지 않는다**

기계 규칙 셋은 이미 반례가 나왔다(계약 문서 표). 프롬프트가 그 셋 중 하나를 조용히 다시
구현하면 안 된다 — 통계는 재료로 싣되 규칙이 아니라고 말한다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 계약 문서·Kotlin 원본, ARTEL-657 이 세운
      `app/qa/{envelope,channel,screen,service}.py` 와 `app/agents/qa/screen.py`,
      기존 단발 agent(`knowledge_query`)의 모양
- [x] **Step 1: 계약의 나머지 절반** — `app/qa/envelope.py`
  - `SCREEN_SELECTOR_VERDICT` 타입
  - `ScreenSelectorChange` · `ScreenSelectorCandidate` — ARTEL-657 이 "필요할 때 세우라" 고
    남긴 두 필드
  - `ScreenSelectorScreenRef.capture_url` · `capture_expires_at`
  - `ScreenSelectorVerdictPayload`
- [x] **Step 2: 판정 agent** — `app/agents/screen_verdict/`
  - `schemas.py` · `prompt.py` · `capture.py` · `agent.py` · `errors.py`
  - `app/prompts/screen_verdict/v1/{system,human}.md`, lock 재생성
- [x] **Step 3: 격리 이음매** — `app/qa/screen_verdict.py`
  - 떨어진 task 로 돌린다. `deliver` 는 즉시 돌아온다
  - 항목 검증: 셋 중 하나인 `match`, 빈/과장 `pattern`, 빈 `reason`, 그리고 **제안이
    싣고 온 후보를 가리키지 않는 항목**은 버린다
  - 전체 상한과 동시 판정 상한. 세션이 닫히면 남은 task 를 끊는다
- [x] **Step 4: 배선** — `app/qa/service.py` 가 시나리오마다 판정기를 세우고
      `SCREEN_SELECTOR_PROPOSAL` 을 그리로 넘긴다. `app/qa/channel.py` 는 답을 실어 보내는
      `answer_screen_selector_proposal` 하나만 늘어난다 (봉투 `sequence` 가 한 곳에서 나야
      하므로)
- [x] **Step 4b: `SCREEN_SETTLED`** (ARTEL-668) — 타입 하나, 라우터 한 줄, 핸들러 하나.
      payload 모델은 `ScreenSelectorProposalPayload` 를 그대로 쓴다 — 저쪽이 세 필드를
      같은 철자로 싣기로 정했다. 제안이 유일한 화면 통로라고 말하던 주석들을 고쳤다
- [x] **Step 5: Tests** — `tests/test_qa_screen_verdict.py`
  - 기계 규칙 셋이 깨진 자리를 픽스처로 만든다 — 이름에 카운터가 붙는 경우, 이름이 같은
    형제 컨트롤 둘, 조작 없이 넘어가는 로딩 화면
  - 지어내기 금지: 후보에 없는 pattern, 정규식 모양, 잘못된 match, 사유 없음
  - 형식 위반은 항목 없는 실패로 답한다
  - QA 런 격리: 판정이 느리거나 터져도 `deliver` 가 즉시 돌아오고 QA 채널이 안 멈춘다
  - `SCREEN_SETTLED`: 제안이 한 장도 안 오는 런에서도 화면이 보인다 / 빈 `discriminator`
    가 사실로 그려진다 / 판정기로 안 넘어간다 / 다른 scene 의 것은 안 그린다

## Validation

- **Commands to run:** `LANGSMITH_TRACING=false env -u OPENROUTER_API_KEY uv run --extra dev pytest`
- **Expected output:** 전부 통과 (`tests/test_config.py::test_settings_can_load_from_env_file`
  는 `OPENROUTER_API_KEY` 가 export 돼 있으면 실패하는 기존 건이라 `env -u` 로 돌린다)

## Risks & Rollback

- **Risks:** 판정이 과하게 넣으면 그 scene 의 화면이 잘게 갈린다. 프롬프트가 "확신 없으면
  무시" 를 지고 있고, 검증이 후보 밖 항목을 버린다. 반대 방향(덜 넣음)은 종전과 같은 상태다
- **Rollback steps:** `git revert`. 제안이 다시 답 없이 지나가고 QA 런은 그대로 돈다

## Open Questions

- `llm_usage.service` 에 이 호출을 위한 값이 없다. 지금은 `QA_RUN` + 그 try 로 단다 —
  실제로 그 try 때문에 난 지출이라 거짓이 아니지만, 따로 세려면 orchestration 쪽
  마이그레이션이 필요하다
- 돌고 있는 orchestration 은 PR #212 를 안 실었으므로 제안이 실제로 도착하는 경로는
  end-to-end 으로 못 봤다
