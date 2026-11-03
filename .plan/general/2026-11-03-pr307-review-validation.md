# TC history 도구 계약과 통합 검증

- read_test_case_history는 게임 조작이 없는 조회 도구이므로 macro 행동 목록 비교에서 제외하고 실제 agent tool 목록에 포함했다.
- 선행 PULSE view·TC snapshot·ACT 관찰 변경을 보존해 통합했고 QA 구조를 v51-pulse-tc-history로 구분했다.
- 수정 전 전체 Windows suite: 3034 통과, 3 실패. 두 실패는 새 도구의 계약 누락이었고, 나머지는 기존 parser 위치 테스트의 Windows 경로 구분자 비교였다.
- 수정 후 history·architecture·도구 목록·macro grammar·phase 관련 234개 통과. Python3.14의 dependency deprecation warning13개.
- 기존 Windows 경로 assertion은 무관한 플랫폼 비교이므로 변경하지 않았다. 실게임 비교 결과는 별도로 기록한다.
