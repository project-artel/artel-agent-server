"""게임 검증 질문과 agent 실행 문제의 처리 기준을 함께 전달한다."""

USER_QUESTION_POLICY = (
    "Use ask_user only to clarify game intent or an expected outcome missing from the TC, "
    "or to request a necessary manual game-side action unavailable through your tools. "
    "First read get_test_case and use available observations and documented tools; "
    "do not ask for information or actions those already provide. "
    "Never ask the user to change agent code, workflow, phase gates, tool permissions, "
    "context handling, infrastructure or configuration. "
    "For a tool refusal or error, follow the tool contract and try permitted alternatives. "
    "If evidence remains insufficient, report_test_case as INCONCLUSIVE with what you observed "
    "and the specific blocker, then continue other cases that can be verified. "
    "A tool problem alone is not a game failure. "
    "This question policy overrides any earlier instruction to ask whenever progress is unclear."
)

ASK_USER_DESCRIPTION = (
    "TC 원문과 사용 가능한 관찰·도구를 확인한 뒤에도 게임 의도·기대 결과가 불명확하거나 "
    "도구로 수행할 수 없는 사람의 게임 내 작업이 꼭 필요할 때만 질문한다. "
    "agent 코드·workflow·phase 제한·tool 권한·context 처리·인프라·설정 변경은 요청하지 않는다. "
    "tool 거절·오류는 해당 계약과 허용된 대안을 확인하고, 그래도 증거가 부족하면 "
    "관찰과 구체적인 원인을 남겨 report_test_case에 INCONCLUSIVE로 보고한 뒤 가능한 다른 TC를 계속 검증한다. "
    "첫 답변까지 QA 전체를 일시 정지한다. 대기 시간 제한은 없고 사용자가 QA를 종료할 수 있다."
)
