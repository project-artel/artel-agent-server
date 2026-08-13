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
