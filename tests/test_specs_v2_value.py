"""What a row says to look for, when the evidence did not settle it."""

from app.specs_v2 import observable
from app.specs_v2.model import Assertion, SourceRef
from app.specs_v2.render import assertion_text


def test_the_apis_own_second_argument_is_not_part_of_the_value() -> None:
    """`SetText(value, syncTextInputBox)` reaches the evidence as `value, true`."""
    assert observable.value_of("ChatWindow.streamingText, true") == "ChatWindow.streamingText"
    assert observable.value_of("_, true") == "_"
    assert observable.value_of("Vector3.zero") == "Vector3.zero"
    assert observable.value_of(None) == ""


def _assertion(operation: str, value) -> Assertion:
    return Assertion(
        "ui-value",
        "Canvas/Chat.label",
        operation,
        value,
        "observable",
        "unresolved",
        "scene",
        SourceRef("r", "e", "m", 0),
    )


def test_an_unsettled_value_is_named_rather_than_printed_as_none() -> None:
    """`표시 값이 None로 갱신된다` reads as a value to check, and is not one."""
    for operation in ("display", "transform", "play", "set"):
        sentence = assertion_text(_assertion(operation, None))
        assert "None" not in sentence
        assert "값 미확정" in sentence


def test_a_settled_value_is_said_as_it_is() -> None:
    assert "`120`" in assertion_text(_assertion("display", "120"))


def test_a_value_keeps_what_follows_the_field_it_resolved() -> None:
    """앞부분이 풀렸다고 뒷부분을 버리면 다른 것을 주장하게 된다.

    `streamingText.Substring(0, i)` 에서 뒤를 버리면 남는 것은 `streamingText` 이고,
    한 글자씩 찍히는 중간 상태가 "본문이 다 나왔다" 로 둔갑한다. 스트리밍 중에 확인하면
    반드시 실패하는 기대다. 덜 주장한 것이 아니라 다른 것을 주장한 것이다.
    """
    from app.specs_v2.discovery import _whole_value

    resolved, resolution, _ = _whole_value(
        "Chat.body.Substring(0, i)", "Canvas/Chat.body", "exact", []
    )

    assert resolved == "Canvas/Chat.body.Substring(0, i)"
    # 호출이 붙었으니 그대로 비교할 오라클이 아니다.
    assert resolution == "ambiguous"


def test_a_value_that_only_reaches_further_into_fields_stays_checkable() -> None:
    """`.transform.position` 은 판독기가 그대로 읽어 준다. 내릴 이유가 없다."""
    from app.specs_v2.discovery import _whole_value

    resolved, resolution, _ = _whole_value(
        "Marker.anchor.transform.position", "Map/Marker.anchor", "exact", []
    )

    assert resolved == "Map/Marker.anchor.transform.position"
    assert resolution == "exact"


def test_a_value_that_is_exactly_the_field_is_left_alone() -> None:
    from app.specs_v2.discovery import _whole_value

    assert _whole_value("Chat.body", "Canvas/Chat.body", "exact", [])[:2] == (
        "Canvas/Chat.body",
        "exact",
    )
