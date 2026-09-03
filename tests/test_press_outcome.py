"""누름 결과를 문장으로 옮기는 자리(ARTEL-777).

`ok` 는 "가상 마우스 상태를 밀었다"는 뜻이지 "무언가 받았다"가 아니었다. 그 하나로
세 가지가 구분되지 않아, 에이전트가 헛손질을 하고도 성공으로 읽고 다음 단계로 갔다.
"""

from app.agents.qa.tools.tool_context import _is_press, _press_outcome


def test_a_press_that_reached_something_names_it() -> None:
    outcome = _press_outcome(
        {"reached": "CardSystem/Card(Clone)", "pointerHeldByPerson": False}
    )

    # pulse 에서 본 이름과 대조할 수 있도록 계층 경로 그대로 싣는다.
    assert outcome == "OnMouseDown → CardSystem/Card(Clone)"


def test_a_press_that_reached_nothing_says_so() -> None:
    """겨냥이 빗나간 것이다. 실패로 만들지는 않는다 — 빈 곳을 누르는 것도 정당한 조작이고,
    그것이 무엇을 뜻하는지는 부르는 쪽이 판단한다."""
    assert _press_outcome({"reached": None, "pointerHeldByPerson": False}) == "닿은 것 없음"


def test_a_pointer_taken_back_is_told_apart_from_an_empty_spot() -> None:
    """둘 다 `reached` 가 비지만 원인이 다르다. 앞은 다시 조준할 일이고, 뒤는 사람이
    마우스를 건드린 것이라 조준을 고쳐도 소용없다."""
    outcome = _press_outcome({"reached": None, "pointerHeldByPerson": True})

    assert outcome == "포인터를 사람이 쥐고 있어 누름이 전해지지 않았다"


def test_an_older_sdk_still_reads_as_ok() -> None:
    """이 값을 모르는 SDK 가 붙어 있어도 종전대로 답한다. 게임 쪽 패키지 갱신과 서버
    배포가 같은 순간일 수 없으므로, 어긋난 동안에도 런이 그대로 돌아야 한다."""
    assert _press_outcome(None) == "ok"
    assert not _is_press(None)
    assert not _is_press({"capturedAt": 12})
