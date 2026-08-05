"""QaCase/QaStep가 Orche scenario.cases[] 계약(ARTEL-254)을 그대로 파싱하는지."""

from app.qa.cases import QaCase, QaScenarioBody, QaStep


def test_parses_orche_cases_contract() -> None:
    # ARTEL-254 agentScenario가 내보내는 형태 그대로.
    raw = {
        "position": 0,
        "title": "상점 진입",
        "category": "RULE",
        "precondition": None,
        "expected": "상점 화면이 열린다",
        "steps": [
            {"id": "s1", "kind": "setup", "assert": False, "intent": "상점으로 이동", "hint": None},
            {"id": "s2", "kind": "guide", "assert": True, "intent": "구매 버튼 누름", "hint": "Enter"},
        ],
    }
    case = QaCase.model_validate(raw)

    assert case.position == 0
    assert case.title == "상점 진입"
    assert case.precondition is None
    assert case.expected == "상점 화면이 열린다"
    assert len(case.steps) == 2

    setup, guide = case.steps
    # `assert` alias가 asserts로 들어오고, setup은 판정 안 함(False).
    assert setup.kind == "setup"
    assert setup.asserts is False
    assert setup.intent == "상점으로 이동"
    assert guide.kind == "guide"
    assert guide.asserts is True
    assert guide.hint == "Enter"


def test_step_defaults_and_unknown_kind_are_lenient() -> None:
    # 최소 필드 + 미지 kind도 파싱을 깨지 않는다(advisory·forward-compat).
    step = QaStep.model_validate({"intent": "뭔가 한다", "kind": "future_kind"})
    assert step.kind == "future_kind"
    assert step.asserts is True  # 기본 판정
    assert step.hint is None

    # 필드명(asserts)로도 채울 수 있다(populate_by_name).
    by_name = QaStep(id="x", asserts=False, intent="i")
    assert by_name.asserts is False


def test_empty_cases_and_steps() -> None:
    case = QaCase.model_validate({"title": "t", "expected": "e"})
    assert case.steps == []


def test_scenario_body_parses_cases_and_falls_back_to_steps() -> None:
    # cases가 실린 새 저작 시나리오.
    with_cases = QaScenarioBody.model_validate(
        {
            "title": "구매",
            "description": "d",
            "cases": [
                {"position": 0, "title": "상점", "expected": "e",
                 "steps": [{"id": "s1", "kind": "setup", "assert": False, "intent": "이동"}]},
            ],
        }
    )
    assert with_cases.title == "구매"
    assert len(with_cases.cases) == 1
    assert with_cases.cases[0].steps[0].kind == "setup"
    assert with_cases.steps == []  # 레거시 스텝은 비어 있음

    # cases 없이 온 구 저작 시나리오 → steps 폴백.
    legacy = QaScenarioBody.model_validate(
        {
            "title": "구", "description": "d",
            "steps": [{"step": 1, "title": "t", "state": "s", "action": "a", "expected": "e"}],
        }
    )
    assert legacy.cases == []
    assert len(legacy.steps) == 1
    assert legacy.steps[0].step == 1
