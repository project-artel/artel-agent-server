# 2026-10-29 — macro 대사 기록 기능 제거

- Date: 2026-10-29
- Jira: ARTEL-1222
- Status: In progress

## Goal

사용자 요청으로 #265를 종료하고 macro 실행 중 지나간 대사를 따로 수집·반환하는 기능을 제거한다. 완료된 #264 종료와 선택적 시각 점검 skill #291을 유지하고 stack·PR 설명을 다시 정리한다.

## Non-goals

macro 실행·일시 정지·재개 제거, 현재 scene의 text 관찰 제거, TC 보고·사용자 피드백·질문 대기 변경, 독립 PR 수정, develop merge.

## Context / Constraints

최신 원격 snapshot을 기준으로 #263은 그대로 두고 #266·#275·#291만 갱신한다. 이미 닫힌 #262·#264·#282·#285 상태를 유지한다. 원격의 다른 변경이나 사용자가 바꾼 draft 상태를 보존하며 expected SHA가 달라지면 중단하고 최신 상태를 재확인한다.

## Approach (Checklist)

- [ ] **Step 0: Recon** 원본 #265의 runtime diff와 현재 shown_text 참조를 확인한다.
- [ ] **Step 1: Implementation** channel의 ShownTextLog·PULSE 누적, MacroSession.text_mark, macro 응답 render_shown_text와 shown 인자를 제거하고 전용 파일·테스트를 제거한다. 기존 실행 결과와 macro fold를 유지한다. 새 prompt v40~v42에서 대사 기록 안내를 제거하고 기존 v1~v39는 변경하지 않는다. 고정 옛 prompt의 안내가 기능을 있다고 가정하지 않도록 macro 활성 실행의 system context에 현재 계약을 명시한다. arch label과 고정 계약 테스트를 갱신한다.
- [ ] **Step 2: Tests** 단계별 compile·macro 실행/재개/fold·channel/PULSE·TC·시각 skill 계약 검사, 최종 전체 pytest. 현재 scene text 유지와 과거 대사가 macro 응답으로 나오지 않는 경계 동작을 확인한다. 실제 게임 반복 QA는 이번 제거 작업에서 수행하지 않는다.
- [ ] **Step 3: Rollout / Rollback** 기존 tip backup, stack292 해제, #265 종료, 후속 base를 develop으로 임시 이동, atomic expected-SHA push, URL 기반 gh stack link로 #263 → #266 → #275 → #291 구성. 설명과 종료 링크·보호 PR의 head/state/base를 확인한다. 현재 review-ready 여부는 바꾸지 않는다.

## Validation

- **Commands to run:** compileall, pytest, Git tree·diff·원격 GraphQL 대조.
- **Expected output:** #265 CLOSED, 실행 코드에서 대사 누적 제거, 현재 text 관찰과 macro 실행·재개 정상, 기존 판본 불변, 새 stack head·base 일치.

## Risks & Rollback

- **Risks:** 이후 수정이 섞인 channel/macro 코드의 과도한 되돌리기, 고정 옛 prompt의 잘못된 안내, 동시 원격 변경.
- **Rollback steps:** 작업 직전 원격 tip과 manifest를 backup에 보존하고 다른 변경을 덮어쓰지 않는다.

## Open Questions

없음. 사용자가 #265 기능의 제거를 명시했다.
