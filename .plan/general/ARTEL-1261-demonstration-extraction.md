# 시연 검증 절차 추출과 QA context 추가

부모 Story ARTEL-1258의 승인된 계획과 고정 계약을 구현한다. 내부 extraction API는 게임 도구 없이 원본 event와 이미지를 structured output으로 정리한다. TC별 승인 절차는 기존 ORDERED 순서와 FREE 선택을 유지하며 실행 context에 첨부한다.

1. camelCase 추출 DTO와 근거 ID 검증, 승인 절차 DTO를 추가한다.
2. versioned prompt와 기존 structured provider를 이용해 초안을 생성한다.
3. 기존 QA 계획에 선택적으로 승인 절차를 연결하고 QA_ARCH_LABEL을 올린다.
4. 원본 누락·잘못된 참조·provider 실패·이전 계약 호환과 두 실행 mode를 테스트한다.

API·단위 테스트로 wiring을 확인한다. 실제 게임의 전후 QA 실행과 정상/실패 fixture 검증은 부모 작업의 통합 단계에서 수행하며 단위 테스트를 품질 개선 효과로 해석하지 않는다. 신규 필드가 없으면 기존 QA 계획 문자열을 유지한다. 모델 재학습·게임 제어·승인 저장은 이 저장소 범위가 아니다.

## 구현과 검증 기록

- `/internal/demonstrations/extract`는 기존 structured provider를 사용하며 게임 도구를 연결하지 않는다. event 참조, 이미지 참조, 초안 완전성을 검증한다. 내부 저장소 이미지는 redirect 없이 2 MiB 상한·15초 timeout으로 읽고 PNG data URL로 전달한다.
- 기본 QA agent와 planner-executer의 ORDERED/FREE context에 승인 snapshot을 연결했다. arch는 각각 `v28-demonstration-procedures`, `pe3-demonstration-procedures`이며 tool fingerprint는 유지했다. 사용한 안내 prompt hash는 run_config에 기록한다.
- 전체 회귀에서 2909개가 통과했다. 기존 `test_the_parser_is_the_only_place_a_definition_is_built` 한 건은 Windows 경로의 역슬래시와 테스트의 슬래시 비교 문제로 제외했다. 이미지 다운로드 보완 이후 관련 163개가 통과했고 최종 DTO 제한 조정 후 시연 테스트 25개도 통과했다. `git diff --check`도 통과했다.
- 실제 LLM 추출은 synthetic fixture로 3회 수행했다. 결과와 발견·수정한 citation 오류는 `ARTEL-1261-llm-smoke.md`에 기록했다. 실제 게임 QA 비교는 부모 작업의 통합 단계에서 검증해야 한다.

## 실제 화면과 QA 통합 검증 기록

- 사람이 실제 UI에서 시연을 추출하고 한국어로 수정한 뒤 procedure `2`, revision `2`를 승인했다. 일본어 초안이 나온 사례를 반영해 추출 언어는 TC를 우선하고 annotation·화면 언어보다 앞서도록 지시했다. prompt lock 갱신 후 관련 93개 테스트가 통과했다. 실제 `openai/gpt-6-luna` synthetic 추출 1회(17.55초)에서 한국어 결과를 확인했다. 이는 언어 선택의 한 사례이며 항상 준수한다는 측정은 아니다.
- backend 계약에 맞춰 단일 event payload와 전체 source를 각각 최대 2,000,000자로 제한하고 target을 최대 10,000자로 제한했다.
- agent를 `127.0.0.1:8098`에서 재시작하고 실제 backend `8097`의 catalog·platform key를 사용했다. 정상 Unity fixture에서 ORDERED run `1` / try `1`을 한 번 실행하여 1/1 PASS, 37.11초를 확인했다. 최초 관찰의 Count: 1을 그대로 믿지 않고 `reset_game` 후 Count: 0을 확인한 다음 한 번 클릭하여 Count: 1과 SDK count=1을 확인했다. 보고서에는 사용한 procedure id 2 revision 2가 기록됐다.
- 실행 축은 model `openai/gpt-6-luna`, prompt `v24`, arch `v28-demonstration-procedures`, fingerprint `7d56243753d1`이다. `run_config.prompt_hashes`에 `demonstration/v1/qa_context`도 남았다. 사용량은 12 calls, input 279,363 / cached 245,488 / output 1,625 tokens, $0.007502이다. 원본 응답은 gitignored `.venv/demo-qa-ordered-normal/`에 저장했다.
- 최초 검증은 승인 절차가 전달되고 현 실행 증거로 정상 판정하는지 확인한 한 번의 통합 실행이다. 이후 FREE·실패 fixture 결과는 아래에 추가했다.

## 판정과 화면만 남기는 annotation 지원

- 사용자 피드백에 따라 사람이 outcome과 evidenceEventId만 남길 수 있도록 추출 지시를 수정했다. `observed`, `expected`, `explanation`은 생략하거나 빈 문자열로 보내는 선택 hint다. event payload는 원래 JSON을 보존하므로 DTO 변경은 필요하지 않았다. 관찰과 판정 이유는 image·SDK evidence에서 추출하고, assertion의 기대 결과는 TC를 기준으로 한다. 설명 부족만을 이유로 재작성을 요구하지 않으며 실제 증거 부족과 TC 충돌은 질문으로 남긴다.
- TC에 시간 제한이 없으면 완료 후 관찰을 조건으로 사용하고, 정밀 latency·frame 동기화가 측정되지 않았다는 이유만으로 질문을 만들지 않게 했다. 필요한 경우 미측정 시간을 `inferences`에 한계로 기록한다. 완료 증거 자체가 없거나 서로 충돌하는 경우는 이 예외에 포함하지 않는다.
- 선택 hint 생략/빈 문자열 두 계약의 회귀 테스트를 추가하고 prompt lock을 갱신했다. 관련 95개 테스트가 통과했다. 실제 설명 없는 annotation의 전체 UI 추출·승인은 backend 재시작 후 별도 통합 검증으로 확인한다. 추가 QA는 보류했다.

## ORDERED/FREE 정상·실패 fixture 검증

실제 Unity 버튼 카운터 build 1에서 각 조건을 한 번씩 실행했다. 정상은 클릭 후 Count가 1이 되고 실패 fixture는 입력이 도착해도 Count가 0으로 남는다. 네 실행 모두 현재 화면과 SDK 관찰로 판정했다. run 1은 procedure 2/revision 2, run 2~4는 설명 없이 판정한 시연에서 승인한 procedure 3/revision 1을 보고서에서 명시했다.

| QA run | mode | fixture | 결과 | 시간 | calls | 비용 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | ORDERED | 정상 | 1/1 PASS | 37.110초 | 12 | $0.007502 |
| 2 | FREE | 정상 | 1/1 PASS | 50.619초 | 10 | $0.007083 |
| 3 | ORDERED | 실패 | 0/1, MAJOR issue 1개 | 30.875초 | 9 | $0.003408 |
| 4 | FREE | 실패 | 0/1, MAJOR issue 1개 | 54.294초 | 10 | $0.003766 |

model은 모두 `openai/gpt-6-luna`, prompt `v24`, arch `v28-demonstration-procedures`다. fingerprint는 ORDERED `7d56243753d1`, FREE `6399537c5953`이며 mode별 도구 차이를 반영한다. 모두 `demonstration/v1/qa_context` prompt hash를 남겼다. 지식은 frozen, content map·cheat·behavior는 off로 실행했다.

run 2의 input/cached/output은 280,848/251,079/1,704, run 3은 243,324/240,148/1,220, run 4는 273,329/270,944/1,518 tokens다. 원본 run/detail/logs, tool_reports, run_config, summary는 gitignored `.venv/demo-qa-{mode}-{normal|fail}/`에 저장했으며 자격증명은 포함하지 않았다.

이는 각 조건 한 번의 실제 연결 검증이다. 변경 전 동일 축 비교 실행과 planner-executer 실제 게임 실행은 하지 않았다. 따라서 성능·품질 개선을 주장하지 않으며 planner-executer는 단위 회귀로만 확인했다. 실제 게임에서 source snapshot이 전달되고 정상/실패를 현재 evidence로 구분하는 범위의 결과다.
