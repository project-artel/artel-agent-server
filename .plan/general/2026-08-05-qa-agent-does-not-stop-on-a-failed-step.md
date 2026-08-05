# 2026-08-05 — QA 에이전트가 스텝 실패로 런을 중단하지 않게 한다

- Date: 2026-08-05
- Jira: ARTEL-242
- Status: Implemented

## Goal

스텝이 기대대로 풀리지 않아도 QA 에이전트가 남은 스텝을 계속 실행하게 한다. 시나리오 스텝을 대본이 아닌 의도로 읽게 하고, 미보고 스텝이 남은 채로 런이 닫히지 않게 한다.

## Non-goals

- 시나리오 생성 에이전트(`app/agents/scenario/`) 변경
- 실패 스텝 자동 복구·재시도 로직 — 판단은 에이전트에게 맡긴다
- 툴 호출 상한(`BASE_TOOL_CALLS`, `TOOL_CALLS_PER_STEP`)과 런 데드라인 조정
- v5 이하 프롬프트 문구 수정

## Context / Constraints

- ARTEL-237(#44) 위에 쌓는다. 그쪽이 v5와 `QaRunState.unreported_steps()`를 이미 추가했으므로 이 작업은 v6이고, 미보고 스텝 계산은 새로 만들지 않고 그 메서드를 쓴다.
- v5까지의 프롬프트는 "어떻게 진행하는가"만 말하고 "안 풀렸을 때 어떻게 하는가"는 말하지 않는다. `report_step`의 실패 경로와 `finish_run` 어느 쪽에도 계속 진행을 요구하는 압력이 없다.
- 결과적으로 첫 실패 지점에서 런이 끝나고, `QaRunner.run_with_deadline`가 `"The agent stopped without closing the run."`로 닫거나 미보고 스텝이 남은 채 종료된다.
- 프롬프트는 `app/prompts/<agent>/<version>/` 구조를 따른다. v2~v5가 모두 그랬듯 기존 버전 문구는 건드리지 않고 새 버전을 추가한다 — `prompt_version=v5`로 태그된 런은 재현 가능해야 한다.
- 프롬프트만으로는 부족하다. 모델이 문단을 흘릴 수 있지만 도구 반환 문구는 매 호출 읽는다. 같은 지시를 양쪽에 건다.
- `finish_run` 되돌려보내기는 상한이 필요하다. 게임이 정말 응답하지 않는 런이 루프에 갇히면 안 된다.

## Approach (Checklist)

- [x] **Step 0: Recon** `app/prompts/qa_run/v5/`, `app/agents/qa/tools.py`의 `report_step`/`finish_run`/`QaRunState`, `tests/test_qa_prompt_version.py`의 버전 회귀 관행 확인
- [x] **Step 1: v6 프롬프트** v5 복사 후 두 문단만 추가 — 시나리오를 의도로 읽는 문단, 실패 스텝이 런의 끝이 아니라는 문단. `vision_directive.md`는 frontmatter만 v6으로.
- [x] **Step 2: 도구 피드백** `QaRunState.finish_attempts` 추가. `report_step`이 남은 스텝이 있을 때 다음 스텝 번호를 지목하고, 실패면 멈출 이유가 아님을 덧붙인다. `finish_run`은 `unreported_steps()`가 비어 있지 않은 첫 호출을 되돌려보내고 두 번째는 무조건 닫는다.
- [x] **Step 3: Tests** 실패 반환의 다음 스텝 지목, 되돌려보내기 1회 + 2회째 종료, v6이 v5를 온전히 포함하는지, 기본 버전이 v6인지
- [x] **Step 4: Rollout / Rollback** 코드 배포만 필요. 문제 시 `qa_prompt_version=v5` 설정으로 프롬프트만 되돌릴 수 있고, 도구 변경은 커밋 revert.

## Validation

- `python -m pytest` — 367 passed (ARTEL-237 브랜치 위)
- 수동: 중간 스텝이 반드시 실패하는 시나리오로 런을 돌려 실패 이후 스텝이 계속 실행되고 `finish_run`으로 닫히는지 타임라인 확인 (미실시)
