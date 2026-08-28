# 2026-08-28 — QA agent 에게 지금 화면을 보여 주고 화면 판정 목록을 고칠 tool 을 준다

- Date: 2026-08-28
- Jira: ARTEL-657
- Status: Implemented

## Goal

QA agent 가 지금 서 있는 screen 이 무엇인지 보고, 지도가 틀렸다고 판단하면 그 자리에서
scene 의 screen selector whitelist 를 고칠 수 있게 한다.

1. orchestration 이 보내 준 screen 판정(screen id + 그 화면을 가른 selector)을 scene view 에
   싣는다. screen 이 바뀐 것도 보인다.
2. tool 둘을 준다 — `include_screen_selector` (이 selector 가 여기서 screen 을 가른다),
   `exclude_screen_selector` (안 가른다). 둘 다 `SCREEN_SELECTOR_RULE` 로 나가고
   `SCREEN_SELECTOR_RESULT` 로 답을 받는다.

## Non-goals

- whitelist 를 저장하고 적용하는 것 — ARTEL-654 (orchestration)
- `SCREEN_SELECTOR_PROPOSAL` 에 답하는 별도 판정 agent — ARTEL-656
- screen 에 이름을 붙이는 것

## Context / Constraints

계약은 orchestration 의 `docs/screen-selector-frames.md` 와
`contentmap/observe/ScreenSelectorFrames.kt` (PR #212, ARTEL-655) 다. 프레임을 새로 짓지
않는다.

- `SCREEN_SELECTOR_RULE` payload: `{ scene, entries: [{match, pattern, screen_defining, reason}] }`
- `match` 는 `selector` · `path` · `subtree` 셋뿐이다. `pattern` 은 정확 문자열이고
  정규식이 아니다 — Kotlin 과 SQL 양쪽에서 평가되기 때문이다
- `SCREEN_SELECTOR_RESULT` payload: `{ type, scene_id, accepted, rejected, folded_screens }`
- tool 설명이 사용 정책의 단일 출처다 (ARTEL-192). prompt 를 건드리지 않는다
- scene 은 agent 가 지금 서 있는 것이어야 한다 — tool 인자로 받지 않고 런의 현재 scene 에서
  채운다
- 넣는 답은 과거 화면을 다시 가르지 않는다. 그 사실이 tool 설명에 있어야 한다

**계약의 구멍(그대로 구현하고 보고한다).** agent 에 screen 판정을 싣는 프레임은
`SCREEN_SELECTOR_PROPOSAL` 하나뿐이고, 그것은 `(scene, selector)` 마다 **평생 한 번만**
나간다. 그래서 판정은 관측마다 오지 않는다. 이 구현은 마지막으로 받은 판정을 그 판정이
가리키는 scene 이름과 함께 들고 있다가, agent 가 그 scene 에 서 있을 때만 그린다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 계약 문서와 Kotlin 원본, `app/qa/{envelope,channel,scene,service}.py`,
      `app/agents/qa/{tools,knowledge}.py`, `app/agents/qa/arch.py` 를 읽었다
- [x] **Step 1: Implementation**
  - `app/qa/envelope.py` — `SCREEN_SELECTOR_{PROPOSAL,RULE,RESULT}` 타입과 payload 모델
  - `app/qa/channel.py` — `on_screen_selector_proposal` · `on_screen_selector_result` ·
    `write_screen_selector_rule`
  - `app/qa/screen.py` — 마지막 screen 판정과 그 렌더
  - `app/qa/scene.py` — scene view 두 갈래(GAME_STATE · pulse) 모두에 screen 블록을 붙인다
  - `app/qa/service.py` — 인입 프레임 둘을 채널로 보낸다
  - `app/agents/qa/screen.py` — tool 설명 둘과 결과 렌더
  - `app/agents/qa/tools.py` — tool 둘
- [x] **Step 2: Tests** — `tests/test_qa_screen_selector.py`
  - 판정이 scene view 에 뜬다 / screen 이 바뀌면 그것이 보인다 / 다른 scene 의 판정은 안 뜬다
  - tool 이 `SCREEN_SELECTOR_RULE` 을 정확한 모양으로 낸다
  - 거절 경로: 관측된 적 없는 selector, 다른 scene 의 selector, 사유 없는 호출
  - tool 이름 목록 테스트를 늘린다
- [x] **Step 3: Rollout** — 마이그레이션 없음. orchestration 이 이 프레임을 모르면
      `SCREEN_SELECTOR_RESULT` 가 안 오고 tool 은 "보냈으나 확인 못 함"으로 답한다

## Validation

- **Commands to run:** `LANGSMITH_TRACING=false env -u OPENROUTER_API_KEY uv run --extra dev pytest`
- **Expected output:** 769 passed (2026-08-28)

## Risks & Rollback

- **Risks:** 판정이 드물게 와서 오래된 값을 지금 화면으로 읽을 위험. scene 이름을 함께
  들고 있다가 다른 scene 에서는 안 그리는 것으로 막고, 블록 자체가 "지도가 마지막으로
  말한 것" 이라고 말한다
- **Rollback steps:** `git revert`. tool 둘이 빠지고 scene view 가 종전대로 돌아간다

## Open Questions

- orchestration 이 관측마다 screen 판정을 실어 주는 프레임이 없다. 후속 이슈가 필요하다
