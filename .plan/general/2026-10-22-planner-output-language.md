# planner_executer 출력 언어 적용

## 문제와 범위

run_config.language는 기록되지만 planner와 evaluator의 네 prompt 경로에 전달되지 않아
ko 실행도 영어로 설명하고 판정한다. WordVenture CI run 136에서 재현했다.

## 변경

- ORDERED·FREE planner/evaluator에 언어 지시문을 추가한다. 원본 game text와 코드 식별자는 보존한다.
- 진행 설명과 고정된 검증 결과를 ko/en으로 선택한다. 원본 parser 오류와 기술 진단은 보존한다.
- 언어 지시문의 hash와 pe3-output-language label로 변경을 기록한다.
- 모델 호출과 channel 출력 경계에서 ko/en 테스트를 추가한다.

## 검증과 남은 작업

planner 테스트 94개 통과. 전체 pytest는 2848개 통과, 기존 Windows 경로 구분자 검사 1개 실패
(`test_qa_macro_model.py:320`, 실제 backslash 경로를 slash 문자열과 비교). pair-review PASS.
실제 Sonnet 한국어 결과는 stage 배포 후 확인해야 한다.
기존 run 136은 pe2-free, Sonnet, prompt v3으로 1회 실행했고 최종 판정 없이 FAILED였다.
변경 후 실제 게임 비교 실행은 아직 수행하지 않았다. 성능 개선이나 TC 통과를 주장하지 않는다.
Jira connector는 재인증 오류로 이슈를 등록하지 못했다.

## 되돌리기

이 변경 commit을 revert한다. API나 저장 데이터 schema는 바꾸지 않는다.
