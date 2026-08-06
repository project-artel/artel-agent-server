"""cases[] → 실행 계획 전개 (ARTEL-261).

Orche가 보낸 저작 Step(cases)이 실행 스텝으로 펴지는지, 그리고 cases 없는 구 저작은
예전과 **바이트 단위로 같은** 첫 메시지를 받는지 — 둘 다 고정한다. setup은 판정 없이
도달할 상태(reach_first)로, guide는 do로, verify는 verify로 가고, 판정 기준은 case의
expected다. Step은 어드바이저리라 hint는 근거로 딸려 나가되 강제가 아니다.
"""

import json

from app.agents.qa.plan import build_execution_plan
from app.agents.scenario import ScenarioStep
from app.qa.cases import QaCase, QaScenarioBody, QaStep


def _one_case_body(**case_kwargs) -> QaScenarioBody:
    return QaScenarioBody(title="T", description="D", cases=[QaCase(**case_kwargs)])


def _cases_json(plan_message: str) -> list[dict]:
    """첫 메시지에 박힌 케이스 JSON을 도로 꺼낸다 — 렌더 문자열이 아니라 구조를 검증한다."""
    block = plan_message.split("Cases to run in order:\n", 1)[1].rsplit("\n\nBegin.", 1)[0]
    return json.loads(block)


# --- cases 경로 ---------------------------------------------------------------


def test_cases_expand_to_one_judged_step_per_case() -> None:
    body = QaScenarioBody(
        title="T",
        description="D",
        cases=[
            QaCase(position=1, title="A", expected="a"),
            QaCase(position=2, title="B", expected="b"),
        ],
    )
    plan = build_execution_plan(body)

    assert plan.uses_cases is True
    # 판정 단위는 case다 — 스텝 수는 case 수, 저작 Step 수가 아니다.
    assert plan.total_steps == 2
    steps = _cases_json(plan.first_message)
    assert [step["step"] for step in steps] == [1, 2]
    assert [step["expected"] for step in steps] == ["a", "b"]


def test_setup_becomes_reach_first_and_guide_becomes_do_and_verify_stays() -> None:
    """kind별로 자리가 갈린다: setup→reach_first(무판정), guide→do, verify→verify."""
    body = _one_case_body(
        position=1,
        title="로그인",
        precondition="타이틀 화면",
        expected="홈 진입",
        steps=[
            QaStep(kind="setup", asserts=False, intent="타이틀로 이동"),
            QaStep(kind="guide", intent="시작 누르기"),
            QaStep(kind="verify", intent="홈 확인"),
        ],
    )
    step = _cases_json(build_execution_plan(body).first_message)[0]

    # precondition과 setup 의도는 도달만 할 상태로 함께 묶인다.
    assert step["reach_first"] == ["타이틀 화면", "타이틀로 이동"]
    assert step["do"] == ["시작 누르기"]
    assert step["verify"] == ["홈 확인"]
    assert step["expected"] == "홈 진입"


def test_unknown_step_kind_is_treated_as_do_not_dropped() -> None:
    """Orche가 새 kind를 보내도 스텝을 잃지 않는다 — 모르면 실행으로 둔다(forward-compat)."""
    body = _one_case_body(
        position=1, expected="e", steps=[QaStep(kind="teleport", intent="문으로")]
    )
    step = _cases_json(build_execution_plan(body).first_message)[0]

    assert step["do"] == ["문으로"]


def test_hint_and_input_and_observe_ride_along_as_advisory_evidence() -> None:
    body = _one_case_body(
        position=1,
        expected="e",
        steps=[
            QaStep(kind="guide", intent="시작", hint="Enter", input="keyboard"),
            QaStep(kind="verify", intent="확인", observe="홈 HUD"),
        ],
    )
    step = _cases_json(build_execution_plan(body).first_message)[0]

    assert step["do"] == ["시작  (try: Enter; via: keyboard)"]
    assert step["verify"] == ["확인  (look at: 홈 HUD)"]


def test_a_case_with_no_authored_steps_is_still_a_judged_step() -> None:
    """저작 Step이 하나도 없어도 case는 expected로 판정할 한 스텝이다."""
    body = _one_case_body(position=1, title="설정", expected="설정 열림")
    plan = build_execution_plan(body)
    step = _cases_json(plan.first_message)[0]

    assert plan.total_steps == 1
    assert "reach_first" not in step and "do" not in step and "verify" not in step
    assert step["expected"] == "설정 열림"


def test_the_cases_message_names_the_advisory_and_setup_rules() -> None:
    """데이터만이 아니라, 그 데이터를 어떻게 읽을지의 실마리도 첫 메시지에 있다."""
    message = build_execution_plan(_one_case_body(position=1, expected="e")).first_message

    assert "SETUP-FAILED" in message
    assert "advisory" in message
    assert "Observe the screen first." in message


# --- 레거시 경로 (바이트 동일) -----------------------------------------------


def _legacy_body() -> QaScenarioBody:
    return QaScenarioBody(
        title="튜토리얼",
        description="튜토리얼 진입을 확인한다",
        steps=[
            ScenarioStep(
                step=1, title="시작", state="타이틀 화면",
                action="시작 버튼을 누른다", expected="튜토리얼 화면으로 넘어간다",
            )
        ],
    )


def test_a_scenario_without_cases_falls_back_to_the_legacy_step_message() -> None:
    """cases가 없으면 예전 러너 `_first_message`와 글자 하나까지 같은 메시지를 만든다.

    이게 어긋나면 프롬프트 v1~v7로 고정해 둔 기존 실행이 조용히 달라진다.
    """
    body = _legacy_body()
    plan = build_execution_plan(body)

    expected_steps = [
        {
            "step": 1, "title": "시작", "state": "타이틀 화면",
            "action": "시작 버튼을 누른다", "expected": "튜토리얼 화면으로 넘어간다",
        }
    ]
    expected_message = (
        "Scenario: 튜토리얼 — 튜토리얼 진입을 확인한다\n\n"
        f"Steps to execute in order:\n{json.dumps(expected_steps, ensure_ascii=False, indent=2)}\n\n"
        "Begin. Observe the screen first."
    )

    assert plan.uses_cases is False
    assert plan.total_steps == 1
    assert plan.first_message == expected_message


def test_an_empty_scenario_plans_zero_steps_without_raising() -> None:
    plan = build_execution_plan(QaScenarioBody(title="빈", description="없음"))

    assert plan.uses_cases is False
    assert plan.total_steps == 0


def test_cases_take_priority_over_legacy_steps_when_both_are_present() -> None:
    """둘 다 오면 cases가 이긴다 — 저작 Step이 실행의 근거다."""
    body = QaScenarioBody(
        title="T",
        description="D",
        steps=[ScenarioStep(step=1, title="s", state="s", action="s", expected="s")],
        cases=[QaCase(position=1, title="c", expected="c")],
    )
    plan = build_execution_plan(body)

    assert plan.uses_cases is True
    assert plan.total_steps == 1
    assert _cases_json(plan.first_message)[0]["expected"] == "c"
