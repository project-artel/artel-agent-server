"""설명에 실린 예시가 parser 를 통과하는지 재는 테스트.

**이 파일이 없으면 예시는 조용히 늙는다.** 문법이 한 칸 바뀔 때 설명 문자열 안의 예시는
그대로 남고, 그 어긋남은 모델이 예시를 베껴 쓰고 거절을 받는 런에서야 처음 보인다.
`descriptions.py` 가 예시를 `EXAMPLES` 목록으로 드는 것은 여기서 돌리기 위해서다.
"""

import pytest

from app.agents.qa.macro.descriptions import (
    EDIT_MACRO_DESCRIPTION,
    EXAMPLES,
    EXAMPLES_BLOCK,
    WRITE_MACRO_DESCRIPTION,
)
from app.agents.qa.macro.parser import macro_definition_from_source


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.name)
def test_every_example_in_the_description_passes_the_parser(example) -> None:
    """모델이 읽는 글과 parser 가 받는 것이 같아야 한다."""
    definition = macro_definition_from_source(example.name, example.source)

    assert definition.entry.name == example.name


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.name)
def test_every_example_reaches_the_tool_the_model_reads(example) -> None:
    """목록에만 있고 설명에 안 실리면 아무도 읽지 않는다."""
    assert example.source.strip() in EXAMPLES_BLOCK
    assert EXAMPLES_BLOCK in WRITE_MACRO_DESCRIPTION


def test_the_examples_ride_on_write_macro_only() -> None:
    """tool 설명은 매 호출마다 전송된다. 두 설명이 나눠 들면 같은 글을 두 번 보낸다.

    쓸 때 읽어야 하는 글이고, 고칠 때는 이미 쓴 글이 눈앞에 있다.
    """
    assert EXAMPLES_BLOCK not in EDIT_MACRO_DESCRIPTION


def test_an_example_shows_a_branch_and_a_helper_and_a_held_key() -> None:
    """예시가 보여 주기로 한 셋이 실제로 거기 있는지 재는 못.

    셋 중 하나가 정리 중에 빠지면 예시는 여전히 통과하지만 가르치는 것이 준다.
    """
    sources = "\n".join(example.source for example in EXAMPLES)

    assert "if absent(" in sources
    assert "def confirm_combination" in sources
    assert "hold_key(" in sources and "release_key(" in sources
