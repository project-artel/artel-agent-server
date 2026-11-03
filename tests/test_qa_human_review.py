"""사람 판정 기록이 QA 첫 메시지에 실리는가 (ARTEL-1257).

기록이 없는 run 의 첫 메시지는 종전과 한 글자도 같아야 한다 — 이 기능은 기록이 있는 TC 에만 붙는다.
"""

import json

from app.agents.qa.runner import HUMAN_REVIEW_RULE, _plan
from app.qa.schemas import QaScenario

REVIEW = {"ai_passed": True, "human_passed": False, "note": "클릭만 하고 영역이 안 열렸다", "game_build_version": "1.0"}


def _scenario(mode: str, reviews: list[dict] | None) -> QaScenario:
    case = {"id": 7, "scene": "Battle", "precondition": "전투 화면", "test_step": "조합 버튼을 누른다", "expected": "조합 영역이 켜진다"}
    if reviews is not None:
        case["reviews"] = reviews
    return QaScenario.model_validate(
        {
            "title": "조합",
            "description": "",
            "execution_mode": mode,
            "steps": [{"action": "조합 버튼을 누른다", "case_id": 7, "case": case}],
        }
    )


def _items(text: str) -> list[dict]:
    start = text.index('{\n')
    document, _ = json.JSONDecoder().raw_decode(text[start:])
    return document['test_cases']


def test_ordered_plan_carries_the_review_on_the_verification_step() -> None:
    text = _plan(_scenario("ORDERED", [REVIEW]))
    assert HUMAN_REVIEW_RULE in text
    assert _items(text)[0]["human_review"] == [
        {"ai_said": "passed", "person_decided": "failed", "note": "클릭만 하고 영역이 안 열렸다", "build": "1.0"}
    ]


def test_free_plan_carries_the_review_on_the_case() -> None:
    text = _plan(_scenario("FREE", [{"ai_passed": False, "human_passed": True}]))
    assert HUMAN_REVIEW_RULE in text
    assert _items(text)[0]["human_review"] == [{"ai_said": "failed", "person_decided": "passed"}]


def test_a_plan_without_reviews_is_unchanged() -> None:
    for mode in ("ORDERED", "FREE"):
        plain = _plan(_scenario(mode, None))
        assert "human_review" not in plain
        assert HUMAN_REVIEW_RULE not in plain
        assert plain == _plan(_scenario(mode, []))


def test_the_switch_leaves_the_plan_as_if_there_were_no_reviews(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_human_review", False)
    for mode in ("ORDERED", "FREE"):
        assert _plan(_scenario(mode, [REVIEW])) == _plan(_scenario(mode, None))


def test_review_keeps_inconclusive_outcome_and_its_original_case_version():
    snapshot = '{"id":7,"expected":"이전 기대 결과"}'
    text = _plan(_scenario("FREE", [{
        "ai_passed": None,
        "human_passed": False,
        "note": "실제 실패를 확인함",
        "case_version_changed": True,
        "case_snapshot": snapshot,
    }]))
    review = _items(text)[0]["human_review"][0]
    assert review["ai_said"] == "inconclusive"
    assert review["person_decided"] == "failed"
    assert review["case_version_changed"] is True
    assert review["reviewed_case_snapshot"] == snapshot
