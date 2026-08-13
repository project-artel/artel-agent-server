"""사람이 읽는 열에 무엇이 나가는가.

식별에 필요한 이름과 사람이 읽을 이름은 다르다. 앞엣것이 뒤엣것으로 새면 시트가
기계의 메모가 된다.
"""

from app.specs_v2 import observable
from app.specs_v2.model import Assertion, SourceRef
from app.specs_v2.render import assertion_text, condition_text


def _assertion(operation: str, value) -> Assertion:
    return Assertion(
        "ui-value",
        "Canvas/Chat.label",
        operation,
        value,
        "observable",
        "exact",
        "scene",
        SourceRef("r", "e", "m", 0),
    )


def test_a_frame_name_does_not_reach_the_sheet() -> None:
    """두 지역 변수를 가르려고 붙인 이름이지, 사람에게 할 말이 아니다."""
    premise = observable.qualify(
        {"kind": "test", "left": "i", "operator": "<", "right": "3"},
        "Story/<Tell>d__1.MoveNext",
    )
    # 식별에는 남아 있다.
    assert premise["left"] == "Story/<Tell>d__1.MoveNext.i"

    said = condition_text(premise)
    assert "d__1" not in said
    assert "i(내부 값) < 3" == said


def test_a_field_kept_its_name_in_the_sheet() -> None:
    kept = {"kind": "test", "left": "Holder.member", "operator": "==", "right": "1"}

    assert condition_text(kept) == "Holder.member == 1"


def test_a_value_that_is_another_things_name_is_said_as_a_relation() -> None:
    """`표시 값이 streamingText 로 갱신된다` 는 그 글자가 화면에 나온다는 말로 읽힌다."""
    named = assertion_text(_assertion("display", "Canvas/Chat.other"))
    assert "와 같아진다" in named
    assert "로 갱신된다" not in named

    written = assertion_text(_assertion("display", '"안녕"'))
    assert "로 갱신된다" in written
    assert "와 같아진다" not in written


def test_a_number_is_a_value_and_not_a_name() -> None:
    for literal in ('"글자"', "120", "-1", "true", "null"):
        assert "로 갱신된다" in assertion_text(_assertion("display", literal))


def test_a_place_is_compared_rather_than_printed() -> None:
    said = assertion_text(_assertion("transform", "MapMove.battle2"))

    assert "와 같은 위치/형태가 된다" in said
