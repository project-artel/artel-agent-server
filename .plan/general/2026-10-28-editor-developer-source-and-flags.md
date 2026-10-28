# 2026-10-28 — editor에 developer macro 본문과 실행 flags 전달

- Date: 2026-10-28
- Jira: ARTEL-1288
- Status: Implemented; validation in progress

## Goal

런 중 editor가 실제 developer macro와 import 의존 본문, 실행 flags를 읽어 추측으로 수정하지 않도록 한다. 사용자 요청대로 기존 Sonnet 런과 수정 작업을 병행한다.

## Non-goals

SDK 경로 해석, developer macro 저장값, editor 발동 조건, prompt 문구와 버전은 변경하지 않는다.

## Context / Constraints

기존 ARTEL-1288 브랜치와 PR #278을 사용한다. baseline 서버는 reload 없이 실행 중이므로 코드 수정 중 기존 런은 그대로 돈다. Sonnet 사용자 키, prompt v5, 동일 빌드와 시나리오로 전후 각 1회 관찰한다. 모델 결과의 통계적 개선을 주장하지 않는다.

## Approach (Checklist)
- [x] **Step 0: Recon** — 핸드오프, editor, executer, developer macro resolver를 확인했다.
- [x] **Step 1: Implementation** — MacroEditor에 LoadedDeveloperMacros를 전달한다. developer action 이름의 #N 접미를 제거하고 usable 목록에서 본문을 찾는다. 기존 imported_names를 사용해 같은 developer namespace 안의 transitive imports를 방문 집합으로 한 번씩 싣는다. bound action 정의의 source도 필요한 인자 context로 보여 준다. 일반 planner action에는 추가하지 않는다. developer namespace의 본문은 참고용이며 대체 def에 필요한 로직을 작성하도록 요청에 명시한다. flags는 place(문자열), message, observed를 기록하고 editor에는 세 필드를 길이 제한과 함께 표시한다. 실행 결과를 작은 선언 타입으로 반환해 flags와 함께 turn에 기록한다. 실패·pause의 flags도 보존한다. editor evidence에 flags 메시지를 그린다. 모델 입력이 달라지므로 PE label을 pe6-editor-evidence로 올리고 이유와 고정 테스트를 추가한다. tool-loop label과 prompt v5는 유지한다.
- [ ] **Step 2: Tests** — developer 본문, 중첩·공유 import, #2 인자 바인딩, 일반 action, flags-only 및 실패·pause 후 flags 전달을 테스트한다. focused 후 전체 pytest를 실행한다. baseline 종료 후 서버를 재시작하고 동일 Sonnet 런으로 검증한다.
- [ ] **Step 3: Rollout / Rollback** — pair-review 후 해당 파일만 커밋·push, PR 검증 기록과 로컬 보고서를 갱신한다. 기존 사용자 수정은 보존한다.

## Validation
- **Commands to run:** `LANGSMITH_TRACING=false .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_editor_repair.py tests/test_planner_executer_steps.py tests/test_planner_executer_engine.py`, 이어 전체 pytest.
- **Expected output:** 모든 회귀 테스트 통과. 런별 model/key_source/prompt_version/agent_arch/fingerprint 확인. step 마지막 STATUS와 editor 발생 여부, 실패 원인을 기록한다. 동일 조건 각 1회로만 해석한다.

## Risks & Rollback
- **Risks:** macro 본문으로 prompt가 커진다. 등록되고 파싱된 developer macro만 실패 action의 의존 관계로 제한한다. 순환/공유 참조는 방문 집합으로 중복 없이 처리한다. flags 기록 형식은 MacroFlag의 실제 필드를 따른다.
- **Rollback steps:** 변경 커밋 revert 또는 editor off.

## Open Questions

FAST REVIEW: PASS, MEDIUM REVIEW: PASS, HEAVY REVIEW: PASS. namespace 안내와 pause 검증 제안을 반영했다. 기록 시 외부 접근이 불가능하면 로컬 검증 보고서에 보존한다.
