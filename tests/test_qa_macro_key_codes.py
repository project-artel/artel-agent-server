"""`press_key`·`hold_key`·`release_key` 의 키가 Unity `KeyCode` 이름인가 (ARTEL-1432).

SDK 는 KeyCode 이름이 아닌 키를 런 중에 거절한다. 그래서 그 매크로는 저장할 때부터 쓸 수 없다. parser 가 거절하고, 거절
문장이 고칠 이름을 댄다 — 시험에서 가장 많이 틀린 `enter` 는 정규화만으로는 `Return` 이 되지 않는다.
"""

import pytest

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.key_codes import KEY_CODE_NAMES, KEY_INPUT_RULES, canonical_key_code, key_code_suggestion
from app.agents.qa.macro.parser import macro_definition_from_source


def _source(call: str) -> str:
    return f"def m() -> None:\n    {call}\n"


def test_the_names_are_unity_key_codes() -> None:
    assert len(KEY_CODE_NAMES) == len(set(KEY_CODE_NAMES)) == 327
    assert "None" not in KEY_CODE_NAMES
    assert {"Return", "Space", "Escape", "LeftShift", "Alpha1", "Keypad0", "UpArrow", "Mouse0"} <= set(KEY_CODE_NAMES)


@pytest.mark.parametrize(
    "name, canonical",
    [("Space", "Space"), ("escape", "Escape"), ("LEFTSHIFT", "LeftShift"), ("enter", None), ("1", None), ("Jump", None)],
)
def test_case_does_not_matter_but_the_name_does(name, canonical) -> None:
    assert canonical_key_code(name) == canonical


@pytest.mark.parametrize(
    "name, suggestion",
    [
        ("enter", "Return"),
        ("left shift", "LeftShift"),
        ("ctrl", "LeftControl"),
        ("esc", "Escape"),
        ("1", "Alpha1"),
        ("numpad 3", "Keypad3"),
        ("up", "UpArrow"),
        ("left_arrow", "LeftArrow"),
        ("Jump", None),
    ],
)
def test_a_wrong_name_gets_the_key_code_it_meant(name, suggestion) -> None:
    assert key_code_suggestion(name) == suggestion


def test_a_key_that_is_not_a_key_code_is_refused_with_the_fix() -> None:
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source("m", _source('press_key("enter", 0.1)'))

    reason = rejected.value.reason
    assert "'enter', which is not a Unity `KeyCode` name. Use `Return`." in reason
    assert KEY_INPUT_RULES in reason


@pytest.mark.parametrize("call", ['hold_key("Jump")', 'release_key("Jump")'])
def test_hold_and_release_are_checked_too(call) -> None:
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source("m", _source(call))

    assert "'Jump', which is not a Unity `KeyCode` name." in rejected.value.reason
    assert "Use `" not in rejected.value.reason.split("name.")[1].split(KEY_INPUT_RULES)[0]


@pytest.mark.parametrize("call", ['press_key("Space", 0.1)', 'press_key("escape", 0.1)', 'hold_key("LeftShift")\n    release_key("leftshift")'])
def test_key_codes_in_any_case_pass(call) -> None:
    macro_definition_from_source("m", _source(call))


def test_a_key_passed_by_name_is_not_checked() -> None:
    macro_definition_from_source("m", "def m(key: string) -> None:\n    press_key(key, 0.1)\n")
