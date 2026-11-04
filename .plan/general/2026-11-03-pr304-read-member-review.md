# PR 304 read_member 재조회와 callee scope 수정

## 범위

- while 조건 평가 전에도 필요한 read_member 값을 준비한다. 입력 뒤 Score가 바뀌면 다음 조건이 새 값을 읽어 반복을 종료해야 한다.
- 새 조회 직전에 해당 이름의 모든 holder cache를 지운다. 응답 누락은 예전 값으로 통과하지 않고 ACTION_REJECTED로 처리한다.
- predicate와 숫자 def의 parameter를 bind한 scope에서 reader를 준비한다. local assignment와 실제 선택된 branch를 순서대로 따라가며, 선택하지 않은 branch는 조회하지 않는다.
- develop의 TC snapshot context와 ACT 관찰 허용을 보존하고 pulse view를 v50-pulse-events-tc-context로 구분한다.

## 검증

- 기존 runner를 파일 수정 없이 메모리에 로드해 새 회귀 테스트만 실행: 9개 모두 실패. while은 첫 입력 뒤에도 50회 반복해 LOOP_LIMIT, 응답 누락은 이전 값 재사용, parameter/local 이름은 read_member 조회 누락으로 실패했다.
- 수정본 macro 전체 및 architecture/phase 묶음: 1,108개 통과, 기존 Windows 경로 표기 assertion 1개 실패. test_qa_macro_model.py:320은 실제 Path가 agents\qa\macro\parser.py인 결과를 agents/qa/macro/parser.py와 비교한다. 해당 테스트는 수정하지 않았다.
- 최종 read_member/architecture/phase/pulse contract: 169개 통과. 새 회귀 테스트 9개를 모두 포함한다.
- git diff --check 통과.
- 실제 Unity QA 전후 비교는 상위 작업에서 별도 수행한다. 이 테스트 결과는 실제 모델의 성능 개선 근거가 아니다.

## Rollback

read_member 수정 commit을 되돌리면 기존 prefetch와 cache 동작으로 복원된다. DB나 wire schema 변경은 없다.
