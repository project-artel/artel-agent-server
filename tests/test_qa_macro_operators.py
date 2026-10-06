"""`(값의 모양, 연산자)` 표 마흔두 칸, 한 군데서.

표가 이 기능의 중심이다. 칸마다 허용이 있거나 거절이 있어서, 새 모양이 들어오면 행이
하나 느는 일이 되고 빠뜨린 칸이 눈에 보인다 — 연산자 하나에 `if` 하나를 다는 모양이면
빠뜨린 칸은 아무 데도 안 보인다.

아래 `EXPECTED` 는 **손으로 적은 기대**이고 `grammar.py` 의 표와 독립이다. 구현의 표로
`parametrize` 하면 표가 자기 자신을 검사하게 되므로, 행 하나를 잘못 고치면 테스트가
함께 틀려 준다.

한 군데인 이유는 같은 표를 두 층이 쓰기 때문이다. 저장 시점 거절은 parser
(`ARTEL-917`)가 하고 평가 시점 거절은 `binding` (`ARTEL-920`)이 하며, 둘이 각자 기대
표를 들면 어긋난다.
"""

import pytest

from app.agents.qa.macro import grammar
from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import MacroOperator, MacroShape
from app.agents.qa.macro.parser import macro_definition_from_source

# 일곱 행 곱하기 여섯 연산자. `True` 가 되는 칸, `False` 가 거절되는 칸이다.
#
# - number 만 순서 비교가 된다. `==` 도 되지만 위태롭다 — 거절이 아니라 권고다.
# - string 과 bool 의 순서 비교는 뜻이 없다.
# - vector 는 `>` 가 성분별인지 크기인지가 추측이고, `==` 는 number 의 위태로움이
#   성분마다 겹친다.
# - object 의 같음은 pulse 키로 가른다.
# - 그 밖의 shape 는 전부 거절한다. 조용히 거짓이 되면 `require` 가 게임 결함처럼
#   실패한다.
EXPECTED: dict[tuple[MacroShape, MacroOperator], bool] = {
    (MacroShape.number, MacroOperator.equal): True,
    (MacroShape.number, MacroOperator.not_equal): True,
    (MacroShape.number, MacroOperator.greater): True,
    (MacroShape.number, MacroOperator.less): True,
    (MacroShape.number, MacroOperator.greater_or_equal): True,
    (MacroShape.number, MacroOperator.less_or_equal): True,
    (MacroShape.string, MacroOperator.equal): True,
    (MacroShape.string, MacroOperator.not_equal): True,
    (MacroShape.string, MacroOperator.greater): False,
    (MacroShape.string, MacroOperator.less): False,
    (MacroShape.string, MacroOperator.greater_or_equal): False,
    (MacroShape.string, MacroOperator.less_or_equal): False,
    (MacroShape.bool_, MacroOperator.equal): True,
    (MacroShape.bool_, MacroOperator.not_equal): True,
    (MacroShape.bool_, MacroOperator.greater): False,
    (MacroShape.bool_, MacroOperator.less): False,
    (MacroShape.bool_, MacroOperator.greater_or_equal): False,
    (MacroShape.bool_, MacroOperator.less_or_equal): False,
    (MacroShape.vector2, MacroOperator.equal): False,
    (MacroShape.vector2, MacroOperator.not_equal): False,
    (MacroShape.vector2, MacroOperator.greater): False,
    (MacroShape.vector2, MacroOperator.less): False,
    (MacroShape.vector2, MacroOperator.greater_or_equal): False,
    (MacroShape.vector2, MacroOperator.less_or_equal): False,
    (MacroShape.vector3, MacroOperator.equal): False,
    (MacroShape.vector3, MacroOperator.not_equal): False,
    (MacroShape.vector3, MacroOperator.greater): False,
    (MacroShape.vector3, MacroOperator.less): False,
    (MacroShape.vector3, MacroOperator.greater_or_equal): False,
    (MacroShape.vector3, MacroOperator.less_or_equal): False,
    (MacroShape.object_, MacroOperator.equal): True,
    (MacroShape.object_, MacroOperator.not_equal): True,
    (MacroShape.object_, MacroOperator.greater): False,
    (MacroShape.object_, MacroOperator.less): False,
    (MacroShape.object_, MacroOperator.greater_or_equal): False,
    (MacroShape.object_, MacroOperator.less_or_equal): False,
    (MacroShape.other, MacroOperator.equal): False,
    (MacroShape.other, MacroOperator.not_equal): False,
    (MacroShape.other, MacroOperator.greater): False,
    (MacroShape.other, MacroOperator.less): False,
    (MacroShape.other, MacroOperator.greater_or_equal): False,
    (MacroShape.other, MacroOperator.less_or_equal): False,
}

CELLS = sorted(EXPECTED, key=lambda cell: (cell[0].value, cell[1].value))


def test_the_matrix_this_file_checks_against_has_every_cell() -> None:
    """한 칸을 빼먹은 기대 표는 조용히 짧은 `parametrize` 가 된다.

    그러면 "한 칸도 빼지 않고 돌린다" 는 말이 거짓이 되는데, 통과하는 테스트는 그것을
    말해 주지 않는다. 그래서 개수를 먼저 단정한다.
    """
    assert len(EXPECTED) == 42
    assert len(EXPECTED) == len(MacroShape) * len(MacroOperator)
    assert set(EXPECTED) == {
        (shape, operator) for shape in MacroShape for operator in MacroOperator
    }


@pytest.mark.parametrize("shape, operator", CELLS)
def test_every_cell_of_the_table_is_what_the_issue_decided(
    shape: MacroShape, operator: MacroOperator
) -> None:
    assert grammar.comparison_allowed(shape, operator) is EXPECTED[(shape, operator)]


@pytest.mark.parametrize("shape", list(MacroShape))
def test_the_operators_a_shape_allows_are_the_ones_a_refusal_names(
    shape: MacroShape,
) -> None:
    """거절 문장이 되는 연산자를 이름으로 댄다. 그 목록이 표와 같아야 한다.

    따로 세면 어긋나고, 어긋난 거절 문장은 모델을 못 되는 연산자로 보낸다.
    """
    named = set(grammar.allowed_operators(shape))
    assert named == {
        operator for operator in MacroOperator if EXPECTED[(shape, operator)]
    }


# --- 저장 시점에 닿는 칸 --------------------------------------------------------
#
# 모양을 저장 시점에 아는 것은 리터럴, 타입을 선언한 이름, 그리고 반환 모양이 정해진
# 읽는 함수다. 그래서 parser 가 닿는 행은 넷이다. `vector2`·`vector3` 는 선언 자리에서
# 이미 거절되고, `other` 는 도착한 값의 모양이라 저장 시점에 쓸 수 없다 — 그 세 행은
# `tests/test_qa_macro_binding.py` 가 평가 시점에 돌린다.

# 양쪽이 같은 모양인 비교. 모양이 달라서 거절되는 경우와 섞이지 않게 한다.
_SAME_SHAPE_COMPARISON: dict[MacroShape, tuple[str, str]] = {
    MacroShape.number: ("speed: float", "speed {operator} 1.5"),
    MacroShape.string: ("label: string", 'label {operator} "x"'),
    MacroShape.bool_: ("ready: bool", "ready {operator} True"),
    MacroShape.object_: ("card: object", 'card {operator} selector("Root[0]/Card[1]")'),
}

_STORE_TIME_CELLS = [
    (shape, operator)
    for shape in _SAME_SHAPE_COMPARISON
    for operator in MacroOperator
]


@pytest.mark.parametrize("shape, operator", _STORE_TIME_CELLS)
def test_the_parser_follows_the_table_for_every_shape_it_can_know(
    shape: MacroShape, operator: MacroOperator
) -> None:
    parameter, condition = _SAME_SHAPE_COMPARISON[shape]
    source = (
        f"def m({parameter}) -> None:\n"
        f'    require({condition.format(operator=operator.value)}, "fix it")\n'
    )
    if EXPECTED[(shape, operator)]:
        macro_definition_from_source("m", source)
        return
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source("m", source)
    reason = rejected.value.reason
    # 거절 문장이 그 모양에서 되는 연산자를 이름으로 댄다. 모델이 한 번에 고치는 데
    # 필요한 것이 그것이고, 안 대면 같은 거절을 여섯 번 받는다.
    assert shape.value in reason
    for allowed in grammar.allowed_operators(shape):
        assert allowed.value in reason


@pytest.mark.parametrize("shape", [MacroShape.vector2, MacroShape.vector3])
def test_the_vector_rows_cannot_be_reached_at_all_from_a_declaration(
    shape: MacroShape,
) -> None:
    """두 행에 닿는 것은 평가 시점에 도착한 값뿐이다.

    v1 에 vector 를 만들 문법도 비교할 연산자도 없어서, 선언해 봐야 할 수 있는 일이
    없다. 그래서 선언 자리에서 먼저 거절하고, 거절 문장이 선언할 수 있는 다섯을 이름으로
    전부 댄다.
    """
    for source in (
        f"def m(v: {shape.value}) -> None:\n    pause_game_time()\n",
        f"def m() -> None:\n    v: {shape.value} = 1\n",
    ):
        with pytest.raises(MacroRejection) as rejected:
            macro_definition_from_source("m", source)
        for declarable in grammar.DECLARABLE_TYPES:
            assert declarable.value in rejected.value.reason
