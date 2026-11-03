# 2026-10-29 — TC 판정 PR 통합과 선택적 시각 점검 skill 전환

- Date: 2026-10-29
- Jira: ARTEL-1270, ARTEL-1221
- Status: In progress

## Goal

#262를 닫고 TC 기대 결과·관찰 근거 보고에 필요한 부분만 #275의 diff에 포함한다. #264의 scene 첫 진입 capture 강제와 모든 capture의 checklist 삽입을 제거하고, 필요할 때 load_skill로 읽는 visual_inspection 절차로 대체한다. 기존 PR의 stack과 설명을 함께 수정한다.

## Non-goals

독립 PR 네 개와 다른 작성자의 PR 수정, develop merge, Unity 게임 조작, 화면 결함 자동 판정 코드 추가.

## Context / Constraints

공유 checkout을 건드리지 않고 별도 clone에서 tree를 준비한다. 기존 head를 backup한 뒤 expected SHA를 확인하고 atomic force-with-lease로만 갱신한다. PR을 unstack하고 #262·#264를 먼저 close한 뒤 head를 갱신해 자동 merged 처리 위험을 피한다. 새 draft PR은 시각 점검 skill 대체 하나만 만들며 기존 ARTEL-1221 맥락을 유지한다.

## Approach (Checklist)

- [x] **Step 0: Recon** #262는 performed와 passed의 step 보고 계약을 바꿨고 #275는 이를 TC 보고로 대체한다. #264는 tool 응답과 scene view에 자동 점검을 추가한다. 기존 load_skill을 재사용한다.
- [ ] **Step 1: Implementation** #263·#265·#266에서 #262와 #264의 변경을 제거해 develop부터 실행 가능하게 만든다. #275는 TC 보고의 최종 계약을 유지하되 불필요한 step 상태를 제거한다. #285 버그 수정과 #282 보존 자료를 유지한다. 새 visual skill PR은 #275 위에 두고 #285·#282를 이어 붙인다. 과거 prompt는 재현용으로 유지하고 새 prompt 판본에서 선택적 절차를 제공한다. vision off에서는 skill을 노출하지 않는다.
- [ ] **Step 2: Tests** 단계별 compile·관련 pytest, 최종 전체 pytest, load_skill 실제 호출·screen capture 응답·scene 전환·vision off·prompt pin 검증. 런타임 shape 변경은 arch label과 계약 test를 갱신한다. 실제 Unity 반복 QA 비교는 이번 PR 재정리에서 수행하지 않았음을 설명에 명시한다.
- [ ] **Step 3: Rollout / Rollback** backup과 lease, 기존 stack 해제, 닫을 PR 종료, 새 head push, draft 대체 PR 생성·attach, URL 기반 gh stack link, head/base/상태/충돌 재확인, PR 설명과 #279 검토 안내 갱신.

## Validation

- **Commands to run:** compileall, pytest, git diff, remote GraphQL snapshot.
- **Expected output:** #262·#264 CLOSED, 독립 네 개 불변, QA stack에서 TC 판정은 #275만 검토, capture 강제 없음, 선택적 skill 제공, 모든 중간 단계 실행 가능.

## Risks & Rollback

- **Risks:** prompt 판본과 stage별 schema의 불일치, 중간 head의 자동 merged 처리, skill off의 기존 inline 동작, 중간 테스트의 과거 계약 의존.
- **Rollback steps:** 이전 stage manifest와 backup branch를 유지한다. 원격 head가 달라지면 push를 중단한다.

## Open Questions

없음. 사용자가 두 PR 종료와 대체 방향을 승인했다.

## Plan review 반영

FAST·MEDIUM의 지적을 반영했다. visual_inspection은 skills=off여도 inline하지 않고 load_skill로만 제공한다. 다른 기존 skill의 설정은 유지하며 vision off 또는 새 skill이 없는 pinned prompt에는 새 tool을 추가하지 않는다. #275에는 expected·관찰 근거·TC 결과 계약만 유지하고 performed 거절 heuristic과 unperformed_pass_refused는 제거한다.

원격 작업 전에 모든 관련 head·base·state를 snapshot과 비교한다. 이전 head 전체를 backup하고 stack을 해제한 뒤 #262·#264를 닫는다. 열린 후속 PR의 base는 모두 develop으로 임시 변경해 이전 base에 새 head가 포함돼 자동 merged되는 상황을 차단한다. develop에 포함되는 head가 없음을 검사한 뒤 atomic lease push한다. 마지막으로 URL 기반 stack link로 최종 순서를 설정한다. 모든 단계는 merge 없이 진행한다.

## 동시 작업의 최신 원격 변경 보존

원격 반영 전 #262 통합, #282 종료·보관 자료 제거, #285의 버그 수정 통합·종료가 별도로 반영된 것을 확인했다. 이전에 준비한 head는 push하지 않았다. 이 세 PR은 닫힌 상태로 유지하고 #263의 최신 head는 그대로 둔다. 현재 stack290의 #264 자동 점검만 #265·#266·#275에서 제거하고 #275 뒤에 새 선택적 skill PR을 연결한다. 새 prompt v36~v39는 기존 v1~v35를 변경하지 않고 누적 보존한다. 이전 설명 중 #282·#285를 stack에 남긴다는 항목은 이 결정으로 대체한다.
