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
    start = text.index("[\n")
    end = text.index("\n]", start) + 2
    return json.loads(text[start:end])


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
