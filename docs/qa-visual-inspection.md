# 선택적 시각 점검 skill 추가

기존 #264의 scene 첫 진입 capture 요구와 모든 capture 응답의 checklist는 제거한다. Agent는 TC가 화면 확인을 요구하거나 시각적 결함이 의심되거나 탐색 QA에서 점검이 필요할 때 `load_skill(name="visual_inspection")`로 절차를 읽는다.

`skills=off`에서는 다른 skill을 기존처럼 inline하고 시각 점검만 요청할 때 읽는다. `skills=on_demand`에서는 다른 skill과 같은 tool로 읽는다. vision이 꺼져 있거나 해당 skill이 없는 옛 prompt를 선택하면 시각 점검 skill을 제공하지 않는다. 선택한 prompt 판본으로 실제 tool 목록과 fingerprint를 기록한다.

절차는 기대한 화면 확인, 최신 screen capture 확인, 필요한 항목 점검, 재현과 근거 기록, 결과 보고 순서다. TC 기대 결과를 위반한 결함은 `report_test_case`의 근거로 보고하고, TC와 관계없는 결함은 `report_issue`로 보고한다. 의도한 디자인인지 불명확한 장면은 결함으로 단정하지 않는다.

기존 prompt 판본은 재현용으로 유지하며 새 실행 기본판은 v42다. 자동 테스트는 로딩·미로딩, vision on/off, skills 설정 두 가지, v30/v39의 실제 tool·fingerprint 일치, scene 전환과 capture 응답을 검증한다. 이번 변경으로 실제 게임에서의 시각 결함 탐지율이 개선됐는지는 측정하지 않았다.

macro 중간 대사 기록은 제거했다. 현재 scene의 최신 text와 screen capture를 근거로 확인하며, 자세한 변경 범위는 [macro 대사 기록 제거](remove-macro-dialogue-history.md)를 따른다.
