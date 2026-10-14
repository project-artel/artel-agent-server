"""`skill_macro.md` 가 parser 와 같은 말을 하는지 재는 테스트.

**이 파일이 없으면 모델이 읽는 글은 조용히 늙는다.** `grammar.py` 에 tool 하나가 늘거나
상한이 한 칸 바뀌면 skill 파일은 그대로 남고, 그 어긋남은 모델이 그 글대로 macro 를 써서
거절을 받는 런에서야 처음 보인다.

두 가지를 잰다. `reference.py` 가 `grammar.py` 로 조립한 글이 skill 파일 안에 글자 그대로
있는지, 그리고 그 파일의 예시가 실제로 parser 를 통과하는지.
"""

import re

import pytest

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import (
    READER_NAMES,
    TOOL_NAMES,
    MacroShape,
    allowed_operators,
    operator_names,
)
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.prompts import (
    TOOL_DESCRIPTION_MAX_CHARS,
    load_skill,
    load_tool_description,
    skill_names,
)

SKILL = "macro"
MACRO_TOOLS = (
    "write_macro",
    "read_macro",
    "edit_macro",
    "register_macro",
    "run_macro",
    "resume_macro",
)
# 예시 안의 ```python 울타리. 진입점 `def` 의 이름으로 parser 를 부른다.
_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)
_ENTRY = re.compile(r"^def (\w+)\(", re.MULTILINE)


def skill_body() -> str:
    return load_skill(SKILL).body


def examples_in_the_skill() -> list[tuple[str, str]]:
    """울타리 안의 글과 그 글의 첫 `def` 이름."""
    found = []
    for block in _FENCE.findall(skill_body()):
        entry = _ENTRY.search(block)
        assert entry is not None, f"a python block in {SKILL} has no entry def:\n{block}"
        found.append((entry.group(1), block))
    return found


def test_the_skill_is_one_of_the_names_load_skill_accepts() -> None:
    assert SKILL in skill_names()


@pytest.mark.parametrize("section", [GRAMMAR, NUMBER_EQUALITY], ids=["grammar", "number"])
def test_the_skill_carries_what_grammar_py_assembles(section: str) -> None:
    """`grammar.py` 가 받는 것과 모델이 읽는 글이 어긋날 자리를 없앤다.

    깨지면 `reference.py` 가 조립한 글을 `skill_macro.md` 에 옮겨 적고 `prompts-lock.json`
    을 다시 써야 한다 — 손으로 한 칸만 고치면 다음 변경에서 또 어긋난다.
    """
    assert section in skill_body(), (
        f"app/prompts/qa_run/*/skill_{SKILL}.md no longer carries what "
        "app/agents/qa/macro/reference.py assembles from grammar.py. Copy the "
        "assembled text into the skill file and re-run "
        "`python -m app.prompts.lock --write`."
    )


def test_the_skill_holds_three_examples() -> None:
    """하나로 줄면 가르치는 것이 준다 — 분기와 helper 가 든 긴 것, 반복과 checkpoint 가 든
    것, 짝을 보여 주는 짧은 것."""
    assert len(examples_in_the_skill()) == 3


def test_every_example_in_the_skill_passes_the_parser() -> None:
    """모델이 베껴 쓸 글이므로, 거절되는 글이 거기 있으면 안 된다."""
    for name, source in examples_in_the_skill():
        try:
            definition = macro_definition_from_source(name, source)
        except MacroRejection as rejected:
            pytest.fail(f"the {name!r} example in skill_{SKILL}.md is refused: {rejected}")
        assert definition.entry.name == name


def test_an_example_shows_a_branch_and_a_helper_and_a_held_key() -> None:
    """예시가 보여 주기로 한 셋이 실제로 거기 있는지 재는 못.

    셋 중 하나가 정리 중에 빠지면 예시는 여전히 통과하지만 가르치는 것이 준다.
    """
    sources = "\n".join(source for _name, source in examples_in_the_skill())

    assert "if absent(" in sources
    assert "def confirm_combination" in sources
    assert "hold_key(" in sources and "release_key(" in sources
    # 반복 둘과 checkpoint. 처음 요청이 이것들이 드러나는 예시였다.
    assert "in find_all(" in sources and "in range(" in sources
    assert "while " in sources
    assert "checkpoint(" in sources


@pytest.mark.parametrize("name", MACRO_TOOLS)
def test_each_macro_tool_description_stays_under_the_cap(name: str) -> None:
    """매 호출마다 나가는 글이다. 넘치면 `validate_prompts` 가 부팅에서 막는다."""
    assert len(load_tool_description(name).body) <= TOOL_DESCRIPTION_MAX_CHARS


@pytest.mark.parametrize("name", MACRO_TOOLS)
def test_each_macro_tool_description_points_at_the_skill(name: str) -> None:
    """상한 때문에 잘린 규칙이 어디 있는지 설명이 말해야 한다.

    안 가리키면 모델은 그 글이 전부라고 읽고, `for` 가 거절인 것도 짝을 풀어야 하는
    것도 모른 채 macro 를 쓴다.
    """
    assert f"{SKILL} skill" in load_tool_description(name).body


# 아래 둘은 `tests/test_qa_macro_tools.py` 에 있었고 `write_macro` 설명을 봤다. 글이
# skill 로 옮겨 가면서 보는 자리만 바뀌었다. `reference.py` 와 맞추는 위쪽 테스트와 겹치는
# 것처럼 보이지만 겹치지 않는다 — 위쪽은 조립한 글과 파일이 같은지를 보고, 이 둘은
# `grammar.py` 의 데이터 자체가 파일 안에 있는지를 본다. `reference.py` 에서 표 한 줄이
# 빠지면 위쪽은 그대로 통과하고 이 둘이 깨진다.


def test_every_tool_and_reader_is_named_in_the_skill() -> None:
    """`parse_target` 의 선례다. 손으로 다시 적으면 parser 와 어긋난다.

    어긋남은 모델이 거절을 받을 때까지 아무 데도 안 보인다.
    """
    body = skill_body()

    for name in TOOL_NAMES:
        assert name in body, name
    for name in READER_NAMES:
        assert name in body, name
    # 일부러 뺀 둘도 이름을 대고 왜인지 말한다.
    assert "click_button" in body and "reset_game" in body
    # 연속값 권고. 거절이 아니라 관용구를 주는 자리다.
    assert "==" in body and "rounds a number to four" in body


def test_the_operator_table_the_model_reads_is_the_table_the_parser_enforces() -> None:
    """표를 손으로 다시 적으면, 한 칸이 바뀔 때 조용히 늙는다.

    그 늙음은 모델이 거절을 받을 때까지 아무 데도 안 보인다.
    """
    body = skill_body()

    for shape in MacroShape:
        assert operator_names(allowed_operators(shape)) in body, shape.value
