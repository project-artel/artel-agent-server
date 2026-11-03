# 2026-11-03 — QA 치트의 precondition 전용 제한 제거

- Date: 2026-11-03
- Jira: ARTEL-1340
- Status: 구현 및 검증 완료, draft PR 준비

## Goal

TC에 별도 제한이 없으면 사용 가능한 치트로 테스트에 필요한 상태에 빠르게 도달한다. 치트가 검증 대상과 관련됐다는 이유만으로 사용을 금지하지 않는다. TC의 동작과 결과를 실제로 확인하고 판정한다.

## Non-goals

SDK, 게임 치트 구현, behavior 정책, tool schema, phase gate, run/TC 허용 목록 변경은 포함하지 않는다. 다른 저장소의 치트 작성 가이드는 이번 실행 prompt 수정에 포함하지 않는다.

## Context / Constraints

`qa_run/v42/skill_cheats.md`는 치트를 precondition에만 쓰도록 제한한다. `tool_run_cheat.md`도 같은 제한을 반복한다. 기존 판본은 변경할 수 없으므로 v42 전체를 v43으로 복사하고 두 본문만 수정한다. prompt 판본은 run에 별도로 기록되며 agent 구조는 바꾸지 않는다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 최신 develop에서 prompt, loader, lock, 기존 game function 테스트 확인.
- [x] **Step 1: Implementation** — v43의 cheats skill과 run_cheat description에서 일반 금지를 제거하고 사용 가능한 치트 우선 사용, TC 제한 준수, 관찰에 따른 판정 안내. Act1 클리어 후 Act2 이동 확인 예시 포함. 다른 본문은 보존.
- [x] **Step 2: Tests** — 기존 prompt loader/lock/version/skills/game function 테스트 실행. v42와 v43의 본문 차이가 두 역할뿐인지 확인. 새 판본 lock 생성 및 기본 판본 assertion을 v43으로 갱신.
- [x] **Step 3: Rollout / Rollback** — 기본 latest는 v43 사용. 명시적으로 고정된 run은 유지. QA_PROMPT_VERSION=v42 또는 per-run prompt_version=v42로 이전 판본 선택 가능.

## Validation

- **Commands to run:** `python -m app.prompts.lock --write`; `python -X utf8 -m pytest tests/test_prompts_loader.py tests/test_prompts_lock.py tests/test_qa_prompt_version.py tests/test_qa_skill_tools.py tests/test_qa_game_functions.py`; `git diff --check`.
- **Expected output:** 기존 판본 hash 보존, 새 판본 유효성 및 기존 허용 목록 테스트 통과. 게임 실행에서 행동이 개선됐다는 주장은 하지 않는다. 실제 결과: 185 passed, 13 dependency deprecation warnings. Windows sandbox의 socketpair 초기화가 멈춰 sandbox 밖에서 실행했으며, 기존 UTF-8 파일을 읽는 테스트에는 -X utf8을 사용했다.

## Risks & Rollback

- **Risks:** model이 치트 호출 성공만으로 TC를 통과시킬 수 있어 실제 동작/결과 관찰과 직접 이동만으로 이전 전환을 증명하지 못한다는 예시를 명시한다. 기존 고정 판본에는 새 지침이 적용되지 않는다.
- **Rollback steps:** 실행 설정을 v42로 고정한다.

## Open Questions

없음. 사용자가 일반 치트 금지 제거와 PR 작성을 요청했다.

## Review

FAST REVIEW: PASS, MEDIUM REVIEW: PASS, HEAVY REVIEW: PASS. Pair review의 판정 문구 finding을 반영해 PASSED/FAILED/INCONCLUSIVE 조건을 구분했고 재검토에서 VERDICT: PASS를 받았다. v42 대비 73개 역할 중 본문이 달라진 것은 skill_cheats와 tool_run_cheat뿐이며, 과거 lock 항목은 유지됐다.
