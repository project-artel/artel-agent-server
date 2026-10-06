"""macro 텍스트가 무엇을 통과시키고 무엇을 거절하나.

파서가 없으면 macro 텍스트를 신뢰할 수 없는 채로 실행 단계에 넘기게 되고, `eval` 도
`exec` 도 없이 macro 를 다루겠다는 설계 전체가 성립하지 않는다. 그래서 이 파일이 재는
것은 "거절하는가" 와 "거절 문장이 고칠 것을 이름으로 대는가" 둘이다. 뒤엣것이 빠지면
모델은 같은 거절을 여러 번 받고 그만큼의 왕복을 잃는다.

`(값의 모양, 연산자)` 표 마흔두 칸은 `tests/test_qa_macro_operators.py` 가 든다.
"""

import pytest

from app.agents.qa.macro.errors import MACRO_NODE_REJECTED, MacroRejection
from app.agents.qa.macro.grammar import (
    PAIRED_TOOLS,
    DECLARABLE_TYPES,
    MAX_CALL_DEPTH,
    MAX_STATEMENTS,
    READER_NAMES,
    TOOL_NAMES,
    MacroType,
)
from app.agents.qa.macro.model import (
    MacroActionStatement,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroFindValue,
    MacroFlagStatement,
    MacroIfStatement,
    MacroRequireStatement,
    MacroSelectorValue,
)
from app.agents.qa.macro.parser import macro_definition_from_source

# 이슈의 예시 macro. statement 아홉이 tool 호출 넷·`require` 셋·대입 둘로 갈린다.
# `require` 세 개에는 remedy 문자열이 달려 있다 — 두 번째 인자는 필수다.
EXAMPLE = '''
def attack_with_combined_card(card_a: object, card_b: object) -> None:
    require(scene() == "Battle", "Open the battle screen before calling this.")
    zone: object = find(name="CombineZone")
    enemy: object = find(label="Enemy")
    drag(card_a, zone)
    drag(card_b, zone)
    require(
        actionable(selector("Root[0]/Canvas[1]/Combine[2]")),
        "Combine both cards before attacking.",
    )
    click(selector("Root[0]/Canvas[1]/Combine[2]"))
    click(selector("Root[0]/Canvas[1]/Attack[3]"))
    require(text(enemy) != "100", "Attack again; the enemy took no damage.")
'''


def parse(source: str, name: str = "m"):
    return macro_definition_from_source(name, source)


def rejection(source: str, name: str = "m") -> str:
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source(name, source)
    assert rejected.value.code == MACRO_NODE_REJECTED
    return rejected.value.reason


# --- 예시가 통과한다 -----------------------------------------------------------


def test_the_example_macro_splits_into_four_actions_three_requires_and_two_assignments() -> None:
    definition = parse(EXAMPLE, "attack_with_combined_card")
    kinds = [statement.kind for statement in definition.entry.statements]

    assert len(kinds) == 9
    assert kinds.count("action") == 4
    assert kinds.count("require") == 3
    assert kinds.count("assign") == 2
    # 호출 넷이 어느 자리였는지까지. 세기만 맞으면 순서가 어긋난 tree 도 통과한다.
    assert kinds == [
        "require",
        "assign",
        "assign",
        "action",
        "action",
        "require",
        "action",
        "action",
        "require",
    ]


def test_the_example_keeps_its_parameters_and_their_declared_types() -> None:
    definition = parse(EXAMPLE, "attack_with_combined_card")

    assert [
        (parameter.name, parameter.declared_type)
        for parameter in definition.entry.parameters
    ] == [("card_a", MacroType.object_), ("card_b", MacroType.object_)]
    assert definition.helpers == ()


def test_a_require_with_no_remedy_is_refused_and_the_same_line_passes_with_one() -> None:
    """예시의 `require` 는 remedy 없이 쓰여 있었다. 둘째 인자는 요구다.

    조건이 거짓일 때 payload 를 읽는 쪽은 나중의 agent 이고, 무엇이 틀렸는지만 있고
    어떻게 하라는 말이 없으면 그 agent 가 추측한다.
    """
    without = 'def m() -> None:\n    require(scene() == "Battle")\n'
    assert "remedy" in rejection(without)

    parse('def m() -> None:\n    require(scene() == "Battle", "Open the battle screen.")\n')


def test_an_empty_remedy_is_refused() -> None:
    assert "empty" in rejection(
        'def m() -> None:\n    require(scene() == "Battle", "   ")\n'
    )


# --- `if` 와 그 조건 -----------------------------------------------------------


def test_if_elif_and_else_all_pass() -> None:
    definition = parse(
        'def m(card: object) -> None:\n'
        '    if text(card) == "shoot":\n'
        "        click(card)\n"
        '    elif text(card) == "fire":\n'
        "        double_click(card)\n"
        "    else:\n"
        "        move_pointer(card)\n"
    )
    outer = definition.entry.statements[0]

    assert isinstance(outer, MacroIfStatement)
    # `elif` 는 Python AST 가 중첩된 `If` 로 표현한다. `If` 를 허용하면 따라오므로
    # 허용으로 적는다.
    assert len(outer.orelse) == 1
    assert isinstance(outer.orelse[0], MacroIfStatement)
    assert outer.orelse[0].orelse[0].kind == "action"


@pytest.mark.parametrize("joiner", ["and", "or"])
def test_a_condition_cannot_be_joined(joiner: str) -> None:
    """평가기가 하나다. 그래서 `if` 의 조건에서도 거절이다."""
    reason = rejection(
        f"def m(card: object) -> None:\n"
        f"    if exists(card) {joiner} actionable(card):\n"
        "        click(card)\n"
    )
    assert joiner in reason
    assert "require" in reason


def test_not_is_refused_and_the_refusal_names_what_to_write_instead() -> None:
    reason = rejection(
        "def m(card: object) -> None:\n"
        "    if not exists(card):\n"
        "        pause_game_time()\n"
    )
    assert "absent" in reason and "!=" in reason


def test_a_name_bound_inside_a_body_cannot_be_used_outside_it() -> None:
    """맨이름은 언제나 parameter 이거나 묶인 이름이라는 불변식을 지키는 거절이다.

    분기가 안 간 채로 몸통 밖에서 그 이름을 쓰면 묶이지 않은 이름이 된다.
    """
    reason = rejection(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        '        zone: object = selector("Root[0]/Zone[1]")\n'
        "        click(zone)\n"
        "    click(zone)\n"
    )
    assert "not bound" in reason


def test_a_name_bound_outside_a_body_can_be_used_inside_it() -> None:
    parse(
        "def m(card: object) -> None:\n"
        '    zone: object = selector("Root[0]/Zone[1]")\n'
        "    if exists(card):\n"
        "        drag(card, zone)\n"
    )


def test_sibling_branches_may_bind_the_same_name() -> None:
    """두 몸통은 겹치지 않고 어느 쪽도 밖으로 안 나간다."""
    parse(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        '        zone: object = selector("Root[0]/Left[1]")\n'
        "        drag(card, zone)\n"
        "    else:\n"
        '        zone: object = selector("Root[0]/Right[2]")\n'
        "        drag(card, zone)\n"
    )


def test_binding_a_name_twice_in_one_body_is_refused() -> None:
    assert "already bound" in rejection(
        "def m() -> None:\n"
        '    zone: object = selector("Root[0]/Left[1]")\n'
        '    zone: object = selector("Root[0]/Right[2]")\n'
    )


def test_an_inner_body_cannot_rebind_a_name_the_outer_one_bound() -> None:
    """가리기이고, parameter 이름을 가리는 대입을 거절하는 것과 같은 이유다."""
    assert "already bound" in rejection(
        "def m(card: object) -> None:\n"
        '    zone: object = selector("Root[0]/Left[1]")\n'
        "    if exists(card):\n"
        '        zone: object = selector("Root[0]/Right[2]")\n'
        "        drag(card, zone)\n"
    )


def test_an_assignment_cannot_hide_a_parameter() -> None:
    assert "parameter" in rejection(
        "def m(card: object) -> None:\n"
        '    card: object = selector("Root[0]/Other[1]")\n'
        "    click(card)\n"
    )


# --- 거절되는 node 들 ----------------------------------------------------------


@pytest.mark.parametrize(
    "source, named",
    [
        (
            "def m(card: object) -> None:\n    for _ in card:\n        click(card)\n",
            "for",
        ),
        (
            "def m(card: object) -> None:\n    while exists(card):\n        click(card)\n",
            "while",
        ),
        ("def m() -> None:\n    if true:\n        pause_game_time()\n", "True"),
        ("def m(card: object) -> None:\n    click_button(card)\n", "click"),
        ("def m() -> None:\n    reset_game()\n", "reset_game"),
        (
            'def m() -> None:\n    report_step(1, True, "done")\n',
            "ask_verdict",
        ),
        ("def m(card: object) -> None:\n    click(card.id)\n", "text("),
        (
            "def m(card: object) -> None:\n    import os\n    click(card)\n",
            "import",
        ),
        (
            "def m(card: object) -> None:\n    def inner(x: object) -> None:\n"
            "        click(x)\n    inner(card)\n",
            "nested",
        ),
        ("def m(card: object) -> None:\n    return\n", "return"),
        (
            'def m(card: object) -> None:\n    "what this does"\n    click(card)\n',
            "comment",
        ),
        (
            'def m(card: object) -> None:\n    label: string = f"{card}"\n',
            "find(",
        ),
    ],
)
def test_a_refused_node_names_what_is_accepted_instead(source: str, named: str) -> None:
    assert named in rejection(source)


def test_a_lowercase_true_is_named_back_as_True() -> None:
    """소문자 `true` 는 `ast.Name` 으로 읽혀 묶인 적 없는 이름으로 거절된다.

    거절 문장이 `True` 를 이름으로 대야 모델이 한 번에 고친다.
    """
    assert "True" in rejection('def m() -> None:\n    ready: bool = true\n')


def test_a_plain_assignment_is_refused_and_the_refusal_names_the_five_types() -> None:
    reason = rejection("def m() -> None:\n    count = 3\n")
    for declared in DECLARABLE_TYPES:
        assert declared.value in reason


def test_a_declaration_with_no_value_is_refused() -> None:
    assert "no value" in rejection("def m() -> None:\n    card: object\n")


def test_an_untyped_parameter_and_an_untyped_return_both_name_the_five_types() -> None:
    for source in (
        "def m(card) -> None:\n    click(card)\n",
        "def m(card: object):\n    click(card)\n",
    ):
        reason = rejection(source)
        if "return" in reason:
            assert "None" in reason
        else:
            for declared in DECLARABLE_TYPES:
                assert declared.value in reason


def test_a_return_type_other_than_None_is_refused() -> None:
    assert "None" in rejection("def m(card: object) -> int:\n    click(card)\n")


# --- 타입과 리터럴 -------------------------------------------------------------


def test_an_int_literal_goes_into_a_float_but_not_the_other_way_round() -> None:
    """wire 가 `3.0` 을 `3` 으로 보내는 마당에 `3.0` 을 요구하면 저자를 괴롭힌다."""
    parse('def m() -> None:\n    speed: float = 3\n    require(speed > 1, "slow down")\n')
    assert "int" in rejection("def m() -> None:\n    count: int = 3.5\n")


def test_a_default_is_allowed_and_checked_against_the_declared_type() -> None:
    parse("def m(card: object, button: int = 0) -> None:\n    click(card, button)\n")
    assert "int" in rejection(
        "def m(card: object, button: int = 0.5) -> None:\n    click(card, button)\n"
    )


def test_a_bool_literal_does_not_pass_as_a_number() -> None:
    """Python 에서 `True` 는 int 이기도 하다. number 행으로 빠지면 안 된다."""
    assert "bool" in rejection("def m() -> None:\n    count: int = True\n")


def test_an_object_name_cannot_hold_a_literal() -> None:
    assert "find" in rejection('def m() -> None:\n    card: object = "Root[0]/Card[1]"\n')


# --- 조준 ---------------------------------------------------------------------


def test_a_name_in_a_target_slot_has_to_be_declared_object() -> None:
    reason = rejection("def m(count: int) -> None:\n    click(count)\n")
    assert "object" in reason


def test_a_string_literal_bound_to_a_name_cannot_aim() -> None:
    """`t: string = "640,360"` 을 묶어 `click(t)` 하면 좌표 금지를 우회한다."""
    reason = rejection(
        'def m() -> None:\n    spot: string = "640,360"\n    click(spot)\n'
    )
    assert "coordinate" in reason


@pytest.mark.parametrize("written", ['"640,360"', '"#12345"', "640"])
def test_a_target_cannot_be_written_out_as_a_value(written: str) -> None:
    reason = rejection(f"def m() -> None:\n    click({written})\n")
    assert "selector" in reason


def test_enter_text_takes_a_target_in_the_macro_grammar() -> None:
    """tool 은 `target_id: int` 를 받지만 macro 문법에서는 `target` 을 받는다."""
    definition = parse(
        'def m(field: object) -> None:\n    enter_text(field, "hello")\n'
    )
    statement = definition.entry.statements[0]

    assert isinstance(statement, MacroActionStatement)
    assert statement.arguments[0].kind == "target"
    assert statement.arguments[1].literal.value == "hello"


def test_a_selector_bound_to_a_name_is_kept_as_a_string() -> None:
    """그 자리에서 풀지 않는다. 문자열에 이름만 붙이고 late binding 을 그대로 둔다.

    즉시 풀면 적어 둔 주소인데도 `STALE_BINDING` 이 나서, 적어 둔 주소면
    `REQUIRE_FAILED`, 이 런에서 찾은 값이면 `STALE_BINDING` 이라는 기준이 깨진다.
    """
    definition = parse(
        'def m() -> None:\n    zone: object = selector("Root[0]/Zone[1]")\n    click(zone)\n'
    )
    assignment = definition.entry.statements[0]

    assert isinstance(assignment, MacroAssignStatement)
    assert isinstance(assignment.value, MacroSelectorValue)
    assert assignment.value.selector == "Root[0]/Zone[1]"


def test_a_find_keeps_the_keywords_it_was_given() -> None:
    definition = parse(
        'def m() -> None:\n    card: object = find(label="shoot", under="Root[0]/Hand[2]")\n'
        "    click(card)\n"
    )
    assignment = definition.entry.statements[0]

    assert isinstance(assignment.value, MacroFindValue)
    assert assignment.value.label.value == "shoot"
    assert assignment.value.under.value == "Root[0]/Hand[2]"
    assert assignment.value.name is None


def test_find_needs_at_least_one_of_label_or_name() -> None:
    assert "label" in rejection(
        'def m() -> None:\n    card: object = find(under="Root[0]")\n'
    )


def test_find_takes_keywords_only() -> None:
    assert "keyword" in rejection(
        'def m() -> None:\n    card: object = find("shoot")\n'
    )


def test_a_find_keyword_takes_a_string_name_and_refuses_an_object_one() -> None:
    """`label=` 은 `PulseObject.text` 를 맞추는 글자다. 객체를 그 자리에 넣을 수 없다.

    `ARTEL-923` 의 예시가 `find(label=card_a)` 를 적는데 `ARTEL-917` 의 예시에서
    `card_a: object` 다. 두 이슈가 어긋난 자리이고, 여기서는 `string` 만 받는다.
    """
    parse(
        'def m(wanted: string) -> None:\n'
        "    card: object = find(label=wanted)\n"
        "    click(card)\n"
    )
    reason = rejection(
        "def m(card_a: object) -> None:\n"
        "    card: object = find(label=card_a)\n"
        "    click(card)\n"
    )
    assert "string" in reason and "object" in reason


def test_a_selector_in_a_comparison_needs_an_object_on_the_other_side() -> None:
    parse(
        'def m(card: object) -> None:\n'
        '    require(card == selector("Root[0]/Card[1]"), "aim at the first card")\n'
    )
    assert "object" in rejection(
        'def m(label: string) -> None:\n'
        '    require(label == selector("Root[0]/Card[1]"), "fix it")\n'
    )


# --- 조건의 왼쪽에 오는 호출 여덟 -----------------------------------------------


def test_member_passes_with_a_target_and_a_name_and_is_refused_without_both() -> None:
    parse(
        'def m(card: object) -> None:\n'
        '    require(member(card, "Card.power") > 5, "pick a stronger card")\n'
    )
    assert "target" in rejection(
        'def m(card: object) -> None:\n    require(member(card) > 5, "fix it")\n'
    )


def test_a_name_declared_object_is_refused_in_a_comparison_against_a_number() -> None:
    """`card_a: object` 가 선언돼 있으면 평가까지 기다리지 않고 저장 때 거절한다."""
    assert "object" in rejection(
        'def m() -> None:\n'
        '    card_a: object = find(label="x")\n'
        '    require(card_a > 5, "fix it")\n'
    )


def test_a_float_name_compared_with_a_number_literal_passes() -> None:
    """number 의 `==` 는 권고가 붙을 뿐 거절이 아니다."""
    parse('def m(speed: float) -> None:\n    require(speed == 1.5, "slow down")\n')
    parse('def m(speed: float) -> None:\n    require(speed == 3, "slow down")\n')


def test_a_string_reader_cannot_be_compared_with_a_number() -> None:
    reason = rejection(
        'def m(card: object) -> None:\n    require(text(card) == 5, "fix it")\n'
    )
    assert "string" in reason and "number" in reason


def test_an_int_name_and_a_float_name_are_one_shape() -> None:
    """선언은 둘을 가르지만 연산자에서는 둘 다 number 다.

    wire 가 네 자리로 반올림해 `3.0` 을 `3` 으로 보내므로, 값만 보고는 가릴 수 없다.
    """
    parse(
        "def m(count: int, speed: float) -> None:\n"
        '    require(count == speed, "they should match")\n'
    )


def test_a_reader_cannot_stand_on_its_own_as_a_statement() -> None:
    assert "require" in rejection("def m(card: object) -> None:\n    text(card)\n")


# --- `flag` 와 `ask_verdict` ---------------------------------------------------


def test_flag_and_ask_verdict_pass_with_the_arguments_they_take() -> None:
    definition = parse(
        'def m(card: object) -> None:\n'
        '    flag("the card bounced back instead of landing")\n'
        '    ask_verdict(3, "the enemy should lose hp when the attack lands")\n'
    )
    flagged, asked = definition.entry.statements

    assert isinstance(flagged, MacroFlagStatement)
    assert flagged.message.startswith("the card bounced")
    assert isinstance(asked, MacroAskVerdictStatement)
    assert asked.step == 3
    assert asked.expected.startswith("the enemy should")


@pytest.mark.parametrize(
    "written",
    [
        "flag()",
        'flag("a", "b")',
        'flag("")',
        "ask_verdict(3)",
        'ask_verdict(0, "x")',
        'ask_verdict(3, "")',
        'ask_verdict("3", "x")',
        'ask_verdict(3.5, "x")',
    ],
)
def test_the_wrong_shape_of_flag_or_ask_verdict_is_refused(written: str) -> None:
    rejection(f"def m() -> None:\n    {written}\n")


# --- helper `def` --------------------------------------------------------------


def test_a_source_with_helpers_keeps_the_entry_point_apart_from_them() -> None:
    definition = parse(
        "def attack(card: object) -> None:\n"
        "    aim(card)\n"
        "    strike()\n"
        "\n"
        "def aim(card: object) -> None:\n"
        "    move_pointer(card)\n"
        "\n"
        "def strike() -> None:\n"
        '    press_key("Space", 0.1)\n',
        "attack",
    )

    assert definition.entry.name == "attack"
    assert sorted(helper.name for helper in definition.helpers) == ["aim", "strike"]
    assert [statement.kind for statement in definition.entry.statements] == [
        "helper",
        "helper",
    ]


def test_a_missing_entry_point_is_refused_and_the_refusal_names_what_was_defined() -> None:
    reason = rejection(
        "def aim(card: object) -> None:\n    move_pointer(card)\n", "attack"
    )
    assert "attack" in reason and "aim" in reason


def test_a_call_to_a_name_that_is_neither_a_tool_nor_a_helper_names_both_lists() -> None:
    reason = rejection("def m(card: object) -> None:\n    smash(card)\n")
    for tool in TOOL_NAMES:
        assert tool in reason
    assert "m" in reason


def test_calling_itself_is_refused() -> None:
    assert "recursion" in rejection("def m(card: object) -> None:\n    m(card)\n")


def test_mutual_recursion_is_refused() -> None:
    assert "recursion" in rejection(
        "def m(card: object) -> None:\n    helper(card)\n"
        "\n"
        "def helper(card: object) -> None:\n    m(card)\n"
    )


def test_a_helper_argument_is_checked_against_its_declared_parameter_type() -> None:
    assert "object" in rejection(
        "def m(count: int) -> None:\n    aim(count)\n"
        "\n"
        "def aim(card: object) -> None:\n    move_pointer(card)\n"
    )


# --- 저장 시점에 정적으로 세는 것 ------------------------------------------------


def _chain(depth: int) -> str:
    """진입점 하나와 helper 가 한 줄로 이어진 source. `depth` 는 진입점을 포함해 센다."""
    names = ["m"] + [f"f{index}" for index in range(1, depth)]
    parts = []
    for index, name in enumerate(names):
        body = (
            f"    {names[index + 1]}(card)\n"
            if index + 1 < len(names)
            else "    click(card)\n"
        )
        parts.append(f"def {name}(card: object) -> None:\n{body}")
    return "\n".join(parts)


def test_a_chain_at_the_depth_limit_passes_and_one_deeper_is_refused() -> None:
    parse(_chain(MAX_CALL_DEPTH))
    reason = rejection(_chain(MAX_CALL_DEPTH + 1))

    assert str(MAX_CALL_DEPTH + 1) in reason
    assert str(MAX_CALL_DEPTH) in reason


def test_a_wide_call_graph_is_judged_without_walking_every_path() -> None:
    """같은 helper 를 두 번 부르는 macro 가 층마다 일을 두 배로 늘리면 안 된다.

    memo 가 빠지면 이것이 2^n 이 된다. helper 스물넷(99줄)에서 5초, 서른에서 5분이
    걸렸고 그것이 `write_macro` tool 호출 안이라 서버 coroutine 을 그만큼 막는다.
    모델이 helper 를 단계마다 하나씩 쓰는 macro 를 내놓는 것은 평범한 경우다.

    벽시계를 재는 테스트가 아니다. 상한을 넘겨 거절되는 것까지가 **끝난다**는 것을 재고,
    memo 가 빠지면 이 테스트는 통과하지 않고 그냥 돌아오지 않는다.
    """
    helpers = 40
    parts = ["def top(card: object) -> None:\n    h0(card)\n    h0(card)\n"]
    for index in range(helpers):
        inner = (
            f"    h{index + 1}(card)\n    h{index + 1}(card)\n"
            if index + 1 < helpers
            else "    click(card)\n"
        )
        parts.append(f"def h{index}(card: object) -> None:\n{inner}")

    reason = rejection("\n".join(parts), "top")

    assert str(helpers + 1) in reason
    assert str(MAX_CALL_DEPTH) in reason


def _flat(count: int) -> str:
    body = "".join("    click(card)\n" for _ in range(count))
    return f"def m(card: object) -> None:\n{body}"


def test_a_macro_at_the_statement_limit_passes_and_one_statement_more_is_refused() -> None:
    parse(_flat(MAX_STATEMENTS))
    reason = rejection(_flat(MAX_STATEMENTS + 1))

    # 센 값과 상한을 함께 댄다. 상한만 말하면 저자는 몇 개를 줄여야 할지 모른다.
    assert str(MAX_STATEMENTS + 1) in reason
    assert str(MAX_STATEMENTS) in reason


def test_the_statement_count_takes_the_larger_branch_of_an_if() -> None:
    """`if` 가 있어도 최대값은 가지들 중 큰 쪽이라 여전히 정적이다.

    `if` 하나와 큰 가지 `MAX_STATEMENTS - 1` 개로 정확히 상한에 닿고, 작은 가지가
    아무리 커도 그것만으로는 안 넘는다.
    """
    def branchy(bigger: int, smaller: int) -> str:
        taken = "".join(f"        click(card)\n" for _ in range(bigger))
        other = "".join(f"        click(card)\n" for _ in range(smaller))
        return (
            "def m(card: object) -> None:\n"
            "    if exists(card):\n"
            f"{taken}"
            "    else:\n"
            f"{other}"
        )

    # 1(`if`) + max(127, 2) = 128.
    parse(branchy(MAX_STATEMENTS - 1, 2))
    # 1 + max(128, 2) = 129.
    assert str(MAX_STATEMENTS + 1) in rejection(branchy(MAX_STATEMENTS, 2))
    # 두 가지를 더하지 않는다. 더하면 64 + 64 + 1 이 상한을 넘겼을 것이다.
    parse(branchy(MAX_STATEMENTS - 1, MAX_STATEMENTS - 1))


def test_a_helper_call_counts_the_call_and_everything_the_helper_turns() -> None:
    """세는 것만 편다, 실행은 안 편다.

    호출 statement 하나와 그 helper 가 도는 statement 를 함께 센다. 세지 않으면 helper
    둘로 상한을 쉽게 우회한다.
    """
    helper_body = "".join("    click(card)\n" for _ in range(MAX_STATEMENTS - 1))
    # 진입점 1(호출) + helper 127 = 128.
    parse(
        "def m(card: object) -> None:\n    aim(card)\n"
        "\n"
        f"def aim(card: object) -> None:\n{helper_body}"
    )
    bigger = "".join("    click(card)\n" for _ in range(MAX_STATEMENTS))
    assert str(MAX_STATEMENTS + 1) in rejection(
        "def m(card: object) -> None:\n    aim(card)\n"
        "\n"
        f"def aim(card: object) -> None:\n{bigger}"
    )


# --- 그 밖의 거절 --------------------------------------------------------------


def test_every_reader_is_named_in_the_refusal_of_a_bad_condition() -> None:
    reason = rejection("def m(card: object) -> None:\n    if 1:\n        click(card)\n")
    for reader in READER_NAMES:
        assert reader in reason


def test_an_empty_source_is_refused() -> None:
    assert "empty" in rejection("   \n")


# --- 눌렀으면 풀어야 하는 tool ------------------------------------------------
#
# tool 을 직접 부르는 agent 는 그 tool 의 docstring("Nothing releases this for you.")을
# 다음 턴에 다시 읽는다. macro 는 글이 저작 시점에 고정이라 읽어 줄 다음 턴이 없고,
# 눌린 채로 남은 키는 그 뒤의 모든 step 을 조용히 바꾼다. 반복이 없어 경로가 유한하므로
# 저장 시점에 센다.


@pytest.mark.parametrize("pair", list(PAIRED_TOOLS), ids=lambda one: one.opens)
def test_holding_without_releasing_is_refused(pair) -> None:
    opener = f"{pair.opens}({_arguments(pair.opens)})"
    closer = f"{pair.closes}({_arguments(pair.closes)})"

    reason = rejection(f"def m() -> None:\n    {opener}\n")
    assert pair.counter in reason and pair.closes in reason

    parse(f"def m() -> None:\n    {opener}\n    {closer}\n")


@pytest.mark.parametrize("pair", list(PAIRED_TOOLS), ids=lambda one: one.opens)
def test_releasing_without_holding_is_refused(pair) -> None:
    opener = f"{pair.opens}({_arguments(pair.opens)})"
    closer = f"{pair.closes}({_arguments(pair.closes)})"

    assert "never took" in rejection(f"def m() -> None:\n    {closer}\n")
    # 합이 0 이어도 순서가 뒤바뀐 것은 거절한다. 그 macro 는 끝에 키를 눌린 채로 둔다.
    assert "never took" in rejection(
        f"def m() -> None:\n    {closer}\n    {opener}\n"
    )


def test_the_two_branches_of_an_if_have_to_leave_the_counter_the_same() -> None:
    """한 분기에서만 누르고 다른 분기에서 푸는 macro 는 거절된다. 그것이 맞는 거절이다.

    경로마다 따로 세고 어느 경로에서든 끝에 0 이 아니면 거절한다는 것이 규칙이고, 두
    가지가 다른 값을 남기면 그런 경로가 반드시 하나 생긴다.
    """
    reason = rejection(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        '        hold_key("W")\n'
        "    else:\n"
        "        click(card)\n"
        '    release_key("W")\n'
    )
    assert "different state" in reason
    # 같은 분기 안에서 누르고 풀면 통과한다.
    parse(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        '        hold_key("W")\n'
        '        release_key("W")\n'
        "    else:\n"
        "        click(card)\n"
    )
    # 두 가지가 같은 값을 남기면 분기 밖에서 풀어도 된다.
    parse(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        '        hold_key("W")\n'
        "    else:\n"
        '        hold_key("S")\n'
        '    release_key("W")\n'
    )


def test_a_helper_that_holds_without_releasing_is_counted_in_its_caller() -> None:
    """카운터는 호출 트리 전체를 지나는 경로를 센다. helper 로 숨길 수 없다."""
    assert "a held key" in rejection(
        "def m() -> None:\n    grab()\n"
        "\n"
        'def grab() -> None:\n    hold_key("W")\n'
    )
    parse(
        "def m() -> None:\n    grab()\n"
        "\n"
        'def grab() -> None:\n    hold_key("W")\n    release_key("W")\n'
    )


def _arguments(tool: str) -> str:
    """짝 tool 을 부르는 데 필요한 최소 인자."""
    return '"W"' if tool in ("hold_key", "release_key") else ""


# --- statement 가 드는 원문 ----------------------------------------------------


def test_a_statement_keeps_the_line_as_it_was_written() -> None:
    """`ast.unparse` 로 되찍지 않는다. 따옴표도 주석도 원문 그대로다."""
    written = (
        "def m(card: object) -> None:\n"
        '    slot: object = selector("CardSlot/0")   # 첫 슬롯\n'
        "    drag(card, slot)  # 끌어 놓는다\n"
    )
    definition = parse(written)

    assert definition.entry.statements[0].source == (
        'slot: object = selector("CardSlot/0")   # 첫 슬롯'
    )
    assert definition.entry.statements[1].source == "drag(card, slot)  # 끌어 놓는다"
    for statement in definition.entry.statements:
        assert statement.source in written


def test_an_if_keeps_only_its_own_line() -> None:
    definition = parse(
        "def m(card: object) -> None:\n"
        "    if exists(card):\n"
        "        click(card)\n"
    )

    assert definition.entry.statements[0].source == "if exists(card):"


def test_a_statement_spread_over_several_lines_keeps_all_of_them() -> None:
    definition = parse(
        "def m(card: object, slot: object) -> None:\n"
        "    drag(\n"
        "        card,\n"
        "        slot,\n"
        "    )\n"
    )

    assert definition.entry.statements[0].source == "drag(\n    card,\n    slot,\n)"


def test_a_statement_outside_a_def_is_refused() -> None:
    assert "def" in rejection('click(selector("Root[0]"))\n')


def test_broken_python_is_refused_with_the_line_it_broke_on() -> None:
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source("m", "def m(card: object) -> None:\n    click(\n")
    assert rejected.value.line is not None
