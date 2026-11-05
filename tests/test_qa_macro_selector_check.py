"""새 macro 에 `#id`·`-`·빈 selector 가 들어가는 것을 쓰는 길에서만 거절하는지 재는 테스트 (ARTEL-1026)."""

import pytest

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.lift import lift_target
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.selector_check import reject_unstable_selectors
from app.qa.pulse import PulseMemory


def target_source(literal: str) -> str:
    return f'def go() -> None:\n    click(selector("{literal}"))\n'


def bind_source(literal: str) -> str:
    return f'def go() -> None:\n    card: object = selector("{literal}")\n    click(card)\n'


@pytest.mark.parametrize("literal", ["#-64624", "#1178", "-", "", "   "])
@pytest.mark.parametrize("build", [target_source, bind_source])
def test_the_write_check_refuses_an_unstable_selector(literal, build) -> None:
    with pytest.raises(MacroRejection) as caught:
        reject_unstable_selectors(build(literal))
    assert caught.value.line is not None
    assert "find(label=" in caught.value.reason


def test_the_write_check_accepts_a_hierarchy_path() -> None:
    reject_unstable_selectors(target_source("Root[0]/Canvas[1]/Button[2]"))


@pytest.mark.parametrize("literal", ["#-64624", "-"])
def test_a_stored_source_with_an_unstable_selector_still_parses(literal) -> None:
    assert macro_definition_from_source("go", target_source(literal)).name == "go"


@pytest.mark.parametrize("raw", ["#-64624", "-", "", "  "])
def test_lift_does_not_accept_an_unstable_selector(raw) -> None:
    assert isinstance(lift_target(raw, PulseMemory()), str)
