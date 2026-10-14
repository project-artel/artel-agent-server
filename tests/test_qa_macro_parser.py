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
    NONE_FITS,
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
    MacroAskCondition,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroContextFindAll,
    MacroContextLiteral,
    MacroContextName,
    MacroContextTarget,
    MacroDefinition,
    MacroFindValue,
    MacroFlagStatement,
    MacroHelperCallStatement,
    MacroIfStatement,
    MacroMatchStatement,
    MacroNameTarget,
    MacroSelectorTarget,
    MacroStringLiteral,
    MacroPredicateCondition,
    MacroRequireStatement,
    MacroReturnStatement,
    MacroSelectorValue,
    MacroWaitUntilStatement,
    MacroWhileStatement,
)
from app.agents.qa.macro.parser import imported_names, macro_definition_from_source

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
    """맨이름은 언제나 parameter 이거나 bind 된 이름이라는 불변식을 지키는 거절이다.

    분기가 안 간 채로 몸통 밖에서 그 이름을 쓰면 bind 되지 않은 이름이 된다.
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
            "def m(card: object) -> None:\n    while exists(card):\n        break\n",
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
            'def m(card: object) -> None:\n    click(card)\n    "what this does"\n',
            "first statement of a `def`",
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
    """소문자 `true` 는 `ast.Name` 으로 읽혀 bind 된 적 없는 이름으로 거절된다.

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


# --- 읽은 값의 bind ---------------------------------------------------------------


def test_a_reader_call_can_be_bound_to_a_name_and_compared_later() -> None:
    """조작 전에 읽은 값을 조작 뒤의 값과 비교한다. QA 가 가장 많이 하는 확인이다."""
    from app.agents.qa.macro.model import MacroAssignStatement, MacroReadValue

    definition = parse(
        "def m(enemy: object) -> None:\n"
        '    before: float = member(enemy, "Enemy.Hp")\n'
        "    click(enemy)\n"
        '    require(member(enemy, "Enemy.Hp") < before, "the attack took no health")\n'
    )
    bound = definition.entry.statements[0]

    assert isinstance(bound, MacroAssignStatement)
    assert isinstance(bound.value, MacroReadValue)
    assert bound.value.call.reader == "member"


def test_a_reading_cannot_be_declared_object() -> None:
    """reader 는 값을 읽지 객체를 찾지 않는다. 그 이름으로 겨눌 수 있게 두면 안 된다."""
    reason = rejection(
        'def m(enemy: object) -> None:\n    hp: object = member(enemy, "Enemy.Hp")\n'
    )

    assert "object" in reason and "find(...)" in reason


@pytest.mark.parametrize(
    "written, declared",
    [
        ("label: int = text(card)", "int"),
        ("there: string = exists(card)", "string"),
        ("where: bool = scene()", "bool"),
    ],
)
def test_a_reader_whose_shape_is_fixed_has_to_be_declared_with_that_shape(
    written: str, declared: str
) -> None:
    """`text` 는 늘 문자열이고 `exists` 는 늘 bool 이다. 실행까지 기다릴 이유가 없다."""
    reason = rejection(f"def m(card: object) -> None:\n    {written}\n")

    assert f"declared `{declared}`" in reason


def test_a_reader_whose_shape_the_game_decides_passes_with_any_value_type() -> None:
    """`member` 의 모양은 게임이 정한다. 맞추는 것은 실행 때로 간다 — 비교와 같다."""
    for declared in ("int", "float", "string", "bool"):
        parse(f'def m(e: object) -> None:\n    v: {declared} = member(e, "Enemy.Hp")\n')


def test_a_name_bound_to_a_reading_cannot_aim() -> None:
    """읽은 문자열로 겨누면 좌표 금지를 우회하는 길이 하나 더 생긴다."""
    reason = rejection(
        "def m(card: object) -> None:\n"
        "    label: string = text(card)\n"
        "    click(label)\n"
    )

    assert "object" in reason


def test_a_name_bound_to_a_reading_fills_an_argument_of_its_type() -> None:
    """읽은 글자를 다른 칸에 그대로 적는 것 — `enter_text` 의 값 자리다."""
    parse(
        "def m(source: object, field: object) -> None:\n"
        "    name: string = text(source)\n"
        "    enter_text(field, name)\n"
    )


# --- 조준 ---------------------------------------------------------------------


def test_a_name_in_a_target_slot_has_to_be_declared_object() -> None:
    reason = rejection("def m(count: int) -> None:\n    click(count)\n")
    assert "object" in reason


def test_a_string_literal_bound_to_a_name_cannot_aim() -> None:
    """`t: string = "640,360"` 을 bind 해 `click(t)` 하면 좌표 금지를 우회한다."""
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
        '    ask_verdict("the enemy should lose hp when the attack lands")\n'
    )
    flagged, asked = definition.entry.statements

    assert isinstance(flagged, MacroFlagStatement)
    assert flagged.message.startswith("the card bounced")
    assert isinstance(asked, MacroAskVerdictStatement)
    assert asked.expected.startswith("the enemy should")
    # step 을 들지 않는다. 판정할 step 은 `run_macro` 호출이 정한다.
    assert "step" not in MacroAskVerdictStatement.model_fields


def test_a_step_number_in_ask_verdict_is_refused_with_what_to_write_instead() -> None:
    """번호를 적게 두면 그 번호가 `run_macro` 의 step 과 어긋날 수 있다.

    어긋났었다. `ask_verdict(4, ...)` 로 등록한 macro 를 `run_macro(step=7)` 로 부르면
    agent 에게 4번 step 을 판정하라고 말했다. 거절 문장이 번호를 빼라고 말해야 모델이 한
    번에 고친다.
    """
    reason = rejection('def m() -> None:\n    ask_verdict(4, "the card lands")\n')

    assert "step number" in reason
    assert "run_macro" in reason
    assert "ask_verdict(<expected>)" in reason


@pytest.mark.parametrize(
    "written",
    [
        "flag()",
        'flag("a", "b")',
        'flag("")',
        "ask_verdict()",
        "ask_verdict(3)",
        'ask_verdict("")',
        'ask_verdict("a", "b")',
        'ask_verdict(expected="x")',
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


# --- 반복과 checkpoint (ARTEL-948, ARTEL-949) ---------------------------------------


def test_a_for_over_find_all_binds_an_object_inside_its_body() -> None:
    from app.agents.qa.macro.model import MacroForStatement

    definition = parse(
        "def m(slot: object) -> None:\n"
        '    for card in find_all(label="shoot", under="Root[0]/Hand[2]"):\n'
        "        drag(card, slot)\n"
    )
    loop = definition.entry.statements[0]

    assert isinstance(loop, MacroForStatement)
    assert loop.declared_type.value == "object"
    assert loop.iterable.kind == "find_all"


def test_a_for_over_range_binds_an_int() -> None:
    definition = parse(
        'def m() -> None:\n    for i in range(3):\n        press_key("Space", 0.1)\n'
    )

    assert definition.entry.statements[0].declared_type.value == "int"


@pytest.mark.parametrize(
    "source, said",
    [
        ('def m() -> None:\n    for x in [1, 2]:\n        press_key("A", 0.1)\n', "find_all"),
        ('def m() -> None:\n    for a, b in range(2):\n        press_key("A", 0.1)\n', "Unpacking"),
        ('def m() -> None:\n    for _ in range(2):\n        press_key("A", 0.1)\n    else:\n        press_key("B", 0.1)\n', "else"),
        ('def m() -> None:\n    for _ in range(99):\n        press_key("A", 0.1)\n', "at most"),
        ('def m() -> None:\n    for _ in range(2.5):\n        press_key("A", 0.1)\n', "whole number"),
        ('def m(s: string) -> None:\n    for _ in range(s):\n        press_key("A", 0.1)\n', "`int`"),
        ('def m() -> None:\n    for c in find_all(under="Root[0]"):\n        click(c)\n', "label="),
        ('def m(e: object) -> None:\n    while exists(e):\n        continue\n', "continue"),
        ('def m(e: object) -> None:\n    while exists(e):\n        click(e)\n    else:\n        click(e)\n', "else"),
        ('def m() -> None:\n    cards: object = find_all(label="a")\n', "for <name> in"),
        ('def m() -> None:\n    find_all(label="a")\n', "for <name> in"),
        ('def m() -> None:\n    checkpoint("")\n', "empty"),
        ('def m() -> None:\n    checkpoint("a", "b")\n', "one string"),
    ],
)
def test_a_wrong_loop_or_checkpoint_is_refused_with_what_to_write(source: str, said: str) -> None:
    assert said in rejection(source)


def test_the_loop_name_ends_with_the_loop() -> None:
    reason = rejection(
        "def m(slot: object) -> None:\n"
        '    for card in find_all(label="a"):\n'
        "        drag(card, slot)\n"
        "    click(card)\n"
    )

    assert "not bound" in reason


def test_a_pass_that_holds_without_releasing_is_refused() -> None:
    """한 회가 0 이면 몇 회를 돌든 0 이다. 그 규칙 하나로 저장 때 판정이 선다."""
    reason = rejection(
        'def m() -> None:\n    for _ in range(2):\n        hold_key("W")\n'
    )

    assert "same pass" in reason


def test_a_pass_that_holds_and_releases_passes() -> None:
    parse(
        "def m() -> None:\n"
        "    for _ in range(2):\n"
        '        hold_key("W")\n'
        '        press_key("D", 0.2)\n'
        '        release_key("W")\n'
    )


@pytest.mark.parametrize(
    "source",
    [
        'def m() -> None:\n    hold_key("W")\n    checkpoint()\n    release_key("W")\n',
        "def m() -> None:\n    pause_game_time()\n    checkpoint()\n    resume_game_time()\n",
        'def m() -> None:\n    hold_key("W")\n    look()\n    release_key("W")\n\n'
        "def look() -> None:\n    checkpoint()\n",
    ],
    ids=["held key", "frozen time", "inside a helper"],
)
def test_a_checkpoint_between_a_press_and_its_release_is_refused(source: str) -> None:
    """거기서 턴이 끝나면 게임이 키를 누른 채로, 시간이 멈춘 채로 턴을 넘긴다."""
    assert "checkpoint" in rejection(source)


def test_a_checkpoint_after_the_release_passes() -> None:
    parse(
        'def m() -> None:\n    hold_key("W")\n    release_key("W")\n    checkpoint()\n'
    )


def test_a_loop_body_counts_once_toward_the_written_length() -> None:
    """반복 몸통은 한 번만 센다. 128 은 적힌 길이의 상한이 됐다."""
    from app.agents.qa.macro.grammar import MAX_STATEMENTS

    body = "".join('        press_key("A", 0.1)\n' for _ in range(MAX_STATEMENTS - 1))
    parse(f"def m() -> None:\n    for _ in range(50):\n{body}")


# --- docstring (ARTEL-984) ------------------------------------------------------


def test_the_entry_docstring_is_kept_and_cleaned() -> None:
    """`inspect.cleandoc` 로 첫 줄 뒤의 공통 들여쓰기와 앞뒤 빈 줄을 걷는다."""
    definition = macro_definition_from_source(
        "m",
        "def m(card: object) -> None:\n"
        '    """Deal one card.\n\n    Use it on the battle screen.\n    """\n'
        "    click(card)\n",
    )

    assert definition.entry.docstring == "Deal one card.\n\nUse it on the battle screen."
    assert [statement.source for statement in definition.entry.statements] == ["click(card)"]


def test_a_helper_may_carry_a_docstring_too() -> None:
    definition = macro_definition_from_source(
        "m",
        'def m() -> None:\n    """Entry."""\n    helper()\n\n\n'
        'def helper() -> None:\n    """Press Space once."""\n    press_key("Space", 0.1)\n',
    )

    assert definition.entry.docstring == "Entry."
    assert definition.helpers[0].docstring == "Press Space once."
    assert len(definition.helpers[0].statements) == 1


def test_a_def_without_a_docstring_has_none_and_a_blank_one_is_none() -> None:
    plain = macro_definition_from_source("m", "def m(card: object) -> None:\n    click(card)\n")
    blank = macro_definition_from_source(
        "m", 'def m(card: object) -> None:\n    """   \n    """\n    click(card)\n'
    )

    assert plain.entry.docstring is None
    assert blank.entry.docstring is None
    assert len(blank.entry.statements) == 1


@pytest.mark.parametrize(
    "source",
    [
        '"""Module docstring."""\ndef m(card: object) -> None:\n    click(card)\n',
        'def m(card: object) -> None:\n    """One."""\n    """Two."""\n    click(card)\n',
        'def m(card: object) -> None:\n    if exists(card):\n        """Inside."""\n'
        "        click(card)\n",
    ],
    ids=["top-level", "second-string", "inside-if"],
)
def test_a_string_anywhere_but_the_first_statement_of_a_def_is_refused(source: str) -> None:
    assert "first statement of a `def`" in rejection(source)


def test_a_body_that_is_only_a_docstring_is_empty() -> None:
    assert "empty body" in rejection('def m() -> None:\n    """Nothing else."""\n')


def test_a_docstring_does_not_count_toward_the_statement_limit() -> None:
    """docstring 은 statement 가 아니다. 한도 꼭 맞는 몸통에 docstring 을 더해도 통과한다."""
    body = "".join('    press_key("Space", 0.1)\n' for _ in range(MAX_STATEMENTS))
    source = f'def m() -> None:\n    """At the limit."""\n{body}'

    definition = macro_definition_from_source("m", source)

    assert len(definition.entry.statements) == MAX_STATEMENTS


# --- `wait_until` 과 predicate (ARTEL-988) -------------------------------------


def test_a_predicate_accepts_a_target_parameter_and_returns_a_condition() -> None:
    definition = parse(
        "def m(card: object) -> None:\n"
        '    require(ready(card), "Wait for the card.")\n\n'
        "def ready(target: object) -> bool:\n"
        "    return actionable(target)\n"
    )

    assert definition.entry.returns == "none"
    assert definition.helpers[0].returns == "bool"
    condition = definition.entry.statements[0].condition
    assert isinstance(condition, MacroPredicateCondition)
    assert condition.callee == "ready"
    assert condition.arguments[0].kind == "target"
    result = definition.helpers[0].statements[0]
    assert isinstance(result, MacroReturnStatement)
    assert result.condition.kind == "call"


def test_a_predicate_accepts_literal_returns_and_a_complete_elif_chain() -> None:
    definition = parse(
        "def m(card: object) -> None:\n"
        '    require(ready(card), "Wait.")\n\n'
        "def ready(card: object) -> bool:\n"
        "    if exists(card):\n"
        "        return True\n"
        "    elif absent(card):\n"
        "        return False\n"
        "    else:\n"
        "        return actionable(card)\n"
    )
    branch = definition.helpers[0].statements[0]
    assert branch.body[0].value is True
    assert branch.orelse[0].body[0].value is False
    assert branch.orelse[0].orelse[0].condition.kind == "call"


def test_a_predicate_must_return_on_every_path() -> None:
    reason = rejection(
        "def m() -> None:\n    require(ready(), \"Wait.\")\n\n"
        "def ready() -> bool:\n"
        '    if scene() == "Battle":\n        return True\n'
        '    elif scene() == "Menu":\n        return False\n'
    )
    assert "every path" in reason and "return" in reason


@pytest.mark.parametrize(
    "body",
    [
        "    return True\n    return False\n",
        "    if exists(target):\n        return True\n    else:\n        return False\n"
        "    return True\n",
    ],
    ids=["after-return", "after-complete-if"],
)
def test_a_statement_after_return_is_refused(body: str) -> None:
    source = (
        "def m(target: object) -> None:\n"
        '    require(ready(target), "Wait.")\n\n'
        "def ready(target: object) -> bool:\n" + body
    )
    assert "cannot run" in rejection(source)


def test_an_entry_point_cannot_be_a_predicate() -> None:
    reason = rejection("def m() -> bool:\n    return True\n")
    assert "entry point" in reason and "-> None" in reason


@pytest.mark.parametrize(
    "body",
    [
        '    click(target)\n',
        '    require(exists(target), "Wait.")\n',
        '    wait_until(exists(target), 1, "Wait.")\n',
        '    for _ in range(1):\n        return True\n',
        '    if exists(target):\n        click(target)\n    return False\n',
    ],
    ids=["action", "require", "wait_until", "for", "nested-action"],
)
def test_a_predicate_body_allows_only_assignment_if_and_return(body: str) -> None:
    source = (
        "def m(target: object) -> None:\n"
        '    require(ready(target), "Wait.")\n\n'
        "def ready(target: object) -> bool:\n" + body
    )
    reason = rejection(source)
    assert "typed assignment" in reason and "`if`" in reason and "`return`" in reason


def test_a_bare_return_in_a_predicate_is_refused() -> None:
    assert "bare `return`" in rejection(
        'def m() -> None:\n    require(ready(), "Wait.")\n\n'
        "def ready() -> bool:\n    return\n"
    )


def test_a_predicate_cannot_be_called_as_a_statement() -> None:
    reason = rejection(
        "def m() -> None:\n    ready()\n\n"
        "def ready() -> bool:\n    return True\n"
    )
    assert "returns a value" in reason and "condition" in reason


def test_a_none_def_cannot_be_used_as_a_condition() -> None:
    reason = rejection(
        'def m() -> None:\n    require(act(), "Wait.")\n\n'
        'def act() -> None:\n    flag("done")\n'
    )
    assert "-> None" in reason and "-> bool" in reason


@pytest.mark.parametrize("kind", ["require", "if", "while", "wait_until"])
def test_a_predicate_can_be_called_in_each_condition_statement(kind: str) -> None:
    statement = {
        "require": '    require(ready(), "Wait.")\n',
        "if": '    if ready():\n        flag("ready")\n',
        "while": '    while ready():\n        flag("ready")\n',
        "wait_until": '    wait_until(ready(), 1, "Wait.")\n',
    }[kind]
    definition = parse(
        "def m() -> None:\n" + statement + "\ndef ready() -> bool:\n    return True\n"
    )
    assert definition.entry.statements[0].condition.callee == "ready"


def test_a_predicate_can_call_another_predicate_in_its_return() -> None:
    definition = parse(
        'def m() -> None:\n    require(first(), "Wait.")\n\n'
        "def first() -> bool:\n    return second()\n\n"
        "def second() -> bool:\n    return True\n"
    )
    assert definition.helpers[0].statements[0].condition.callee == "second"


@pytest.mark.parametrize(
    "predicates",
    [
        "def first() -> bool:\n    return first()\n",
        "def first() -> bool:\n    return second()\n\n"
        "def second() -> bool:\n    return first()\n",
    ],
    ids=["direct", "mutual"],
)
def test_predicate_recursion_is_refused(predicates: str) -> None:
    reason = rejection('def m() -> None:\n    require(first(), "Wait.")\n\n' + predicates)
    assert "recursion" in reason


def test_call_depth_counts_predicates() -> None:
    def source(predicates: int) -> str:
        """진입점이 `p0` 을 부르고 `p0` 이 `p1` 을 부르는 식의 사슬.

        깊이는 `predicates` 에 진입점 하나를 더한 수다.
        """
        parts = ['def m() -> None:\n    require(p0(), "Wait.")\n']
        for index in range(predicates):
            returned = f"p{index + 1}()" if index + 1 < predicates else "True"
            parts.append(f"def p{index}() -> bool:\n    return {returned}\n")
        return "\n".join(parts)

    parse(source(MAX_CALL_DEPTH - 1))
    reason = rejection(source(MAX_CALL_DEPTH))
    assert str(MAX_CALL_DEPTH + 1) in reason


def test_statement_count_includes_predicate_statements() -> None:
    def source(assignments: int) -> str:
        body = "".join(f"    n{index}: int = {index}\n" for index in range(assignments))
        return (
            'def m() -> None:\n    require(ready(), "Wait.")\n\n'
            "def ready() -> bool:\n" + body + "    return True\n"
        )

    parse(source(MAX_STATEMENTS - 2))
    assert str(MAX_STATEMENTS + 1) in rejection(source(MAX_STATEMENTS - 1))


def test_wait_until_keeps_a_float_seconds_and_remedy() -> None:
    definition = parse(
        'def m() -> None:\n    wait_until(scene() == "Battle", 1, "Open Battle.")\n'
    )
    statement = definition.entry.statements[0]
    assert isinstance(statement, MacroWaitUntilStatement)
    assert statement.seconds == 1.0
    assert statement.remedy == "Open Battle."


@pytest.mark.parametrize(
    "written, expected",
    [
        ('wait_until(scene() == "Battle", True, "Wait.")', "literal"),
        ('wait_until(scene() == "Battle", 0, "Wait.")', "greater than 0"),
        ('wait_until(scene() == "Battle", 31, "Wait.")', "30"),
        ('wait_until(scene() == "Battle", seconds, "Wait.")', "stored"),
        ('wait_until(scene() == "Battle", seconds=1, remedy="Wait.")', "=="),
        ('wait_until(scene() == "Battle", 1)', "three"),
        ('wait_until(scene() == "Battle", 1, "   ")', "empty"),
    ],
)
def test_wait_until_rejects_invalid_arguments(written: str, expected: str) -> None:
    assert expected in rejection(f"def m(seconds: int = 1) -> None:\n    {written}\n")


@pytest.mark.parametrize(
    "source",
    [
        'def m() -> None:\n    pause_game_time()\n'
        '    wait_until(scene() == "Battle", 1, "Wait.")\n    resume_game_time()\n',
        'def m() -> None:\n    pause_game_time()\n    look()\n    resume_game_time()\n\n'
        'def look() -> None:\n    wait_until(scene() == "Battle", 1, "Wait.")\n',
    ],
    ids=["direct", "helper"],
)
def test_wait_until_while_game_time_is_frozen_is_refused(source: str) -> None:
    reason = rejection(source)
    assert "pause_game_time" in reason and "resume_game_time" in reason
    assert "time out" in reason


def test_wait_until_while_a_key_is_held_is_allowed() -> None:
    parse(
        'def m() -> None:\n    hold_key("W")\n'
        '    wait_until(scene() == "Battle", 1, "Wait.")\n'
        '    release_key("W")\n'
    )


# --- `from macros import ...` -------------------------------------------------
#
# 가져온 macro 는 이미 파싱된 정의로 들어온다. 등록된 macro 를 읽는 것은 async 이고 파서는
# 순수 함수라서, tool 계층이 `imported_names` 로 이름을 먼저 뽑아 정의를 모아 넘긴다.

END_TURN = '''
def end_turn() -> None:
    """End the player's turn so the enemy acts."""
    click(selector("Root[0]/Canvas[1]/EndTurn[2]"))
'''

CAST_PAIR_FIRST_TARGET = '''
def cast_pair_first_target(spell: object, element: object, target_name: string) -> None:
    """Combine a spell card with an element card and cast it on the named target."""
    drag(spell, element)
    target: object = find(name=target_name)
    click(target)
'''

ORCHESTRATION = '''
from macros import end_turn, cast_pair_first_target

def play_undead_boss() -> None:
    """Play the undead boss fight in TurnBattleScene, one decision per turn."""
    player: object = find(name="Word")
    while scene() == "TurnBattleScene":
        boss: object = find(name="Boss_Undead(Clone)")
        hp: float = member(player, "Player.Hp")
        match decide("What should the player do this turn?", hp=hp, boss=boss, hand=find_all(name="Card(Clone)")):
            case "end_turn":
                end_turn()
            case "cast_pair_first_target":
                spell: object = find(name="Card(Clone)", label="Shoot")
                element: object = find(name="Card(Clone)", label="Fire")
                cast_pair_first_target(spell, element, "Boss_Undead(Clone)")
'''


def registered(**sources: str) -> dict[str, MacroDefinition]:
    """이름 → 그 이름으로 파싱한 정의. tool 계층이 넘기는 `imports` 와 같은 모양이다."""
    return {name: parse(source, name) for name, source in sources.items()}


def battle_macros() -> dict[str, MacroDefinition]:
    return registered(
        end_turn=END_TURN, cast_pair_first_target=CAST_PAIR_FIRST_TARGET
    )


def parse_with(
    source: str, imports: dict[str, MacroDefinition], name: str = "m"
) -> MacroDefinition:
    return macro_definition_from_source(name, source, imports=imports)


def rejection_with(
    source: str, imports: dict[str, MacroDefinition], name: str = "m"
) -> str:
    with pytest.raises(MacroRejection) as rejected:
        macro_definition_from_source(name, source, imports=imports)
    assert rejected.value.code == MACRO_NODE_REJECTED
    return rejected.value.reason


def test_an_orchestration_macro_imports_registered_macros_in_import_order() -> None:
    definition = parse_with(ORCHESTRATION, battle_macros(), "play_undead_boss")

    assert [imported.name for imported in definition.imports] == [
        "end_turn",
        "cast_pair_first_target",
    ]
    assert definition.imported("end_turn").entry.docstring == (
        "End the player's turn so the enemy acts."
    )
    loop = definition.entry.statements[1]
    assert isinstance(loop, MacroWhileStatement)
    match = loop.body[2]
    assert isinstance(match, MacroMatchStatement)
    assert match.source.startswith('match decide("What should the player do this turn?"')
    assert match.decide.question == "What should the player do this turn?"
    assert [case.option for case in match.cases] == [
        "end_turn",
        "cast_pair_first_target",
    ]

    end_turn_call = match.cases[0].body[0]
    assert isinstance(end_turn_call, MacroHelperCallStatement)
    assert end_turn_call.callee == "end_turn" and end_turn_call.arguments == ()

    cast_call = match.cases[1].body[2]
    assert isinstance(cast_call, MacroHelperCallStatement)
    assert [argument.parameter for argument in cast_call.arguments] == [
        "spell",
        "element",
        "target_name",
    ]
    assert cast_call.arguments[0].target == MacroNameTarget(name="spell")
    assert cast_call.arguments[2].literal.value == "Boss_Undead(Clone)"


def test_an_imported_name_must_be_a_key_of_imports_and_extra_keys_are_ignored() -> None:
    source = (
        "from macros import end_turn\n\n"
        "def m() -> None:\n    end_turn()\n"
    )
    reason = rejection_with(source, {})
    assert "`end_turn` is imported but no registered macro has that name" in reason
    assert "Register it first, or remove it from the import" in reason

    definition = parse_with(source, battle_macros())
    assert [imported.name for imported in definition.imports] == ["end_turn"]


@pytest.mark.parametrize(
    "source, said",
    [
        ("import macros\n\ndef m() -> None:\n    end_turn()\n", "plain `import x`"),
        (
            "def m() -> None:\n    end_turn()\n\nfrom macros import end_turn\n",
            "after a `def`",
        ),
        ("from os import path\n\ndef m() -> None:\n    end_turn()\n", "`from os import"),
        ("from .macros import end_turn\n\ndef m() -> None:\n    end_turn()\n", "relative"),
        ("from macros import *\n\ndef m() -> None:\n    end_turn()\n", "import *"),
        (
            "from macros import end_turn as finish\n\ndef m() -> None:\n    finish()\n",
            "as finish",
        ),
        (
            "from macros import end_turn\nfrom macros import end_turn\n\n"
            "def m() -> None:\n    end_turn()\n",
            "imported twice",
        ),
        (
            "from macros import end_turn\n\ndef m() -> None:\n    end_turn()\n\n"
            "def end_turn() -> None:\n    flag(\"done\")\n",
            "both imported and written as a `def`",
        ),
        ("from macros import m\n\ndef m() -> None:\n    flag(\"done\")\n", "imports itself"),
        (
            "def m() -> None:\n    from macros import end_turn\n    end_turn()\n",
            "inside a `def`",
        ),
    ],
    ids=[
        "plain import",
        "after def",
        "other module",
        "relative",
        "star",
        "alias",
        "twice",
        "collides with def",
        "itself",
        "inside body",
    ],
)
def test_a_wrong_import_is_refused_with_the_form_that_is_accepted(
    source: str, said: str
) -> None:
    imports = battle_macros() | registered(m='def m() -> None:\n    flag("done")\n')
    reason = rejection_with(source, imports)
    assert said in reason
    if said not in ("imported twice", "both imported and written as a `def`", "imports itself"):
        assert "from macros import a, b" in reason


def test_imported_names_lists_the_imports_in_order_once_each() -> None:
    assert imported_names(ORCHESTRATION) == ("end_turn", "cast_pair_first_target")
    assert imported_names(
        "from macros import b, a\nfrom macros import b, c\n\ndef m() -> None:\n    a()\n"
    ) == ("b", "a", "c")
    assert imported_names("def m() -> None:\n    click(card)\n") == ()
    # 문법 오류는 본 파싱이 줄 번호와 함께 거절한다.
    assert imported_names("from macros import a\n\ndef m( -> None:\n") == ()


def test_an_imported_call_binds_its_arguments_against_the_imported_entry() -> None:
    imports = battle_macros()
    header = "from macros import cast_pair_first_target\n\n"

    missing = rejection_with(
        header + "def m(spell: object, element: object) -> None:\n"
        "    cast_pair_first_target(spell, element)\n",
        imports,
    )
    assert "missing target_name" in missing

    wrong_type = rejection_with(
        header + "def m(count: int, element: object) -> None:\n"
        '    cast_pair_first_target(count, element, "Boss")\n',
        imports,
    )
    assert "object" in wrong_type

    unknown_keyword = rejection_with(
        header + "def m(spell: object, element: object) -> None:\n"
        '    cast_pair_first_target(spell, element, target="Boss")\n',
        imports,
    )
    assert "no `target=` argument" in unknown_keyword


def test_an_imported_macro_is_not_a_condition() -> None:
    reason = rejection_with(
        "from macros import end_turn\n\n"
        'def m() -> None:\n    if end_turn():\n        flag("done")\n',
        battle_macros(),
    )
    assert "imported macro" in reason and "-> bool" in reason


def test_an_unknown_call_names_the_imported_macros_too() -> None:
    reason = rejection_with(
        "from macros import end_turn\n\ndef m() -> None:\n    smash()\n",
        battle_macros(),
    )
    assert "end_turn" in reason


RAIN_PAIR = '''
def rain_pair() -> None:
    """Drop one rain card pair."""
    click(selector("Root[0]/Rain[1]"))
'''


def test_call_depth_runs_through_an_imported_macro() -> None:
    """가져온 macro 의 깊이는 그 macro 자신의 깊이다. orchestration → fight → rain 은 3 이다.

    그 사이에 helper 를 끼워 상한까지 채우면 통과하고, 하나 더 끼우면 거절된다.
    """
    rain = registered(rain_pair=RAIN_PAIR)

    def fight(waves: int) -> MacroDefinition:
        names = [f"wave{index}" for index in range(waves)] + ["rain_pair"]
        helpers = "".join(
            f"\ndef {name}() -> None:\n    {names[index + 1]}()\n"
            for index, name in enumerate(names[:-1])
        )
        return parse_with(
            "from macros import rain_pair\n\n"
            'def fight_rain_stage() -> None:\n    """Fight the rain stage."""\n'
            f"    {names[0]}()\n{helpers}",
            rain,
            "fight_rain_stage",
        )

    orchestration = (
        "from macros import fight_rain_stage\n\n"
        "def m() -> None:\n    fight_rain_stage()\n"
    )
    parse_with(orchestration, {"fight_rain_stage": fight(0)})
    parse_with(orchestration, {"fight_rain_stage": fight(MAX_CALL_DEPTH - 3)})

    reason = rejection_with(
        orchestration, {"fight_rain_stage": fight(MAX_CALL_DEPTH - 2)}
    )
    assert f"{MAX_CALL_DEPTH + 1} deep" in reason
    assert "m → fight_rain_stage → wave0 → " in reason
    assert reason.count(" → ") == MAX_CALL_DEPTH


def test_the_statement_count_runs_through_an_imported_macro() -> None:
    """가져온 macro 호출은 호출 하나에 그 macro 의 entry 총수를 더한다."""
    flags = "".join(f'    flag("{index}")\n' for index in range(MAX_STATEMENTS - 1))
    heavy = registered(heavy=f'def heavy() -> None:\n    """Many flags."""\n{flags}')
    caller = "from macros import heavy\n\ndef m() -> None:\n    heavy()\n"

    parse_with(caller, heavy)
    reason = rejection_with(caller.replace("    heavy()\n", '    heavy()\n    flag("x")\n'), heavy)
    assert str(MAX_STATEMENTS + 1) in reason


def test_an_imported_macro_that_holds_without_releasing_is_counted_in_its_caller() -> None:
    """카운터는 가져온 macro 안까지 센다.

    등록된 macro 는 저장 때 이미 짝이 맞는다. 그래서 짝이 안 맞는 정의를 손으로 만들어,
    부르는 쪽의 검사가 가져온 정의 안을 걷는다는 것만 잰다.
    """
    paired = parse('def grab() -> None:\n    hold_key("W")\n    release_key("W")\n', "grab")
    unpaired = paired.model_copy(
        update={
            "entry": paired.entry.model_copy(
                update={"statements": paired.entry.statements[:1]}
            )
        }
    )
    caller = "from macros import grab\n\ndef m() -> None:\n    grab()\n"

    assert "a held key" in rejection_with(caller, {"grab": unpaired})
    parse_with(caller, {"grab": paired})
    # 가져온 macro 가 누른 것을 부르는 쪽이 풀면 짝이 맞는다.
    parse_with(caller + '    release_key("W")\n', {"grab": unpaired})


def test_a_checkpoint_inside_an_imported_macro_between_a_press_and_its_release_is_refused() -> None:
    look = registered(look='def look() -> None:\n    """Look around."""\n    checkpoint()\n')
    reason = rejection_with(
        "from macros import look\n\n"
        'def m() -> None:\n    hold_key("W")\n    look()\n    release_key("W")\n',
        look,
    )
    assert "checkpoint" in reason and "a held key" in reason
    parse_with(
        "from macros import look\n\n"
        'def m() -> None:\n    hold_key("W")\n    release_key("W")\n    look()\n',
        look,
    )


def test_an_imported_helper_name_does_not_collide_with_a_local_helper() -> None:
    """두 macro 가 같은 이름의 helper 를 들어도 섞이지 않는다."""
    imported = registered(
        stage=(
            'def stage() -> None:\n    """Stage."""\n    step()\n\n'
            'def step() -> None:\n    hold_key("W")\n    release_key("W")\n'
        )
    )
    definition = parse_with(
        "from macros import stage\n\n"
        "def m() -> None:\n    stage()\n    step()\n\n"
        'def step() -> None:\n    flag("local")\n',
        imported,
    )
    assert [helper.name for helper in definition.helpers] == ["step"]


def test_an_orchestration_macro_survives_a_json_round_trip() -> None:
    definition = parse_with(ORCHESTRATION, battle_macros(), "play_undead_boss")
    assert MacroDefinition.model_validate_json(definition.model_dump_json()) == definition


# --- `match decide(...)` ------------------------------------------------------


def decide_source(match_line: str, cases: str | None = None, parameters: str = "") -> str:
    cases = cases if cases is not None else (
        '        case "end_turn":\n            end_turn()\n'
        '        case "cast_pair_first_target":\n            flag("cast")\n'
    )
    return (
        "from macros import end_turn, cast_pair_first_target\n\n"
        f"def m({parameters}) -> None:\n"
        '    boss: object = find(name="Boss")\n'
        '    hp: float = member(boss, "Hp")\n'
        f"    {match_line}\n" + cases
    )


def test_decide_context_takes_all_five_kinds_of_value() -> None:
    definition = parse_with(
        decide_source(
            'match decide("What now?", boss=boss, door=selector("Root[0]/Door[1]"), '
            'hand=find_all(name="Card(Clone)"), hp=hp, goal="win", turns=3, '
            "hurry=True, ratio=-0.5):"
        ),
        battle_macros(),
    )
    match = definition.entry.statements[2]
    assert isinstance(match, MacroMatchStatement)
    context = {argument.key: argument for argument in match.decide.context}

    assert list(context) == ["boss", "door", "hand", "hp", "goal", "turns", "hurry", "ratio"]
    assert context["boss"] == MacroContextTarget(key="boss", target=MacroNameTarget(name="boss"))
    assert context["door"] == MacroContextTarget(
        key="door", target=MacroSelectorTarget(selector="Root[0]/Door[1]")
    )
    assert isinstance(context["hand"], MacroContextFindAll)
    assert context["hand"].find.name == MacroStringLiteral(value="Card(Clone)")
    assert context["hp"] == MacroContextName(key="hp", name="hp", declared_type=MacroType.float_)
    assert isinstance(context["goal"], MacroContextLiteral)
    assert context["goal"].literal.value == "win"
    assert context["turns"].literal.value == 3
    assert context["hurry"].literal.value is True
    assert context["ratio"].literal.value == -0.5


def test_a_context_name_can_be_a_parameter_or_a_loop_name() -> None:
    definition = parse_with(
        "from macros import end_turn, cast_pair_first_target\n\n"
        "def m(target: object, rounds: int) -> None:\n"
        '    for card in find_all(name="Card"):\n'
        '        match decide("Play this card?", card=card, target=target, rounds=rounds):\n'
        '            case "end_turn":\n                end_turn()\n'
        '            case "cast_pair_first_target":\n'
        '                cast_pair_first_target(card, target, "Boss")\n',
        battle_macros(),
    )
    match = definition.entry.statements[0].body[0]
    kinds = [argument.kind for argument in match.decide.context]
    assert kinds == ["target", "target", "name"]


def test_decide_with_no_context_passes() -> None:
    definition = parse_with(decide_source('match decide("What now?"):'), battle_macros())
    assert definition.entry.statements[2].decide.context == ()


def test_a_name_bound_in_a_case_ends_with_that_case() -> None:
    reason = rejection_with(
        decide_source(
            'match decide("What now?"):',
            '        case "end_turn":\n'
            '            card: object = find(name="Card")\n'
            "            end_turn()\n"
            '        case "cast_pair_first_target":\n'
            '            cast_pair_first_target(card, boss, "Boss")\n',
        ),
        battle_macros(),
    )
    assert "`card` is not bound" in reason
    # 형제 `case` 가 같은 이름을 bind 하는 것은 된다.
    parse_with(
        decide_source(
            'match decide("What now?"):',
            '        case "end_turn":\n'
            '            card: object = find(name="Card")\n'
            "            click(card)\n"
            '        case "cast_pair_first_target":\n'
            '            card: object = find(name="Card")\n'
            '            cast_pair_first_target(card, boss, "Boss")\n',
        ),
        battle_macros(),
    )


_TWO_CASES = (
    '        case "end_turn":\n            end_turn()\n'
    '        case "cast_pair_first_target":\n            flag("cast")\n'
)


@pytest.mark.parametrize(
    "match_line, cases, said",
    [
        ("match scene():", _TWO_CASES, "exactly one subject"),
        ("match decide():", _TWO_CASES, "exactly one positional argument"),
        ('match decide("a", "b"):', _TWO_CASES, "exactly one positional argument"),
        ('match decide("  "):', _TWO_CASES, "cannot be empty"),
        ('match decide(f"{hp}"):', _TWO_CASES, "f-string"),
        ("match decide(hp):", _TWO_CASES, "plain string"),
        (
            'match decide("What now?"):',
            '        case "end_turn" if exists(boss):\n            end_turn()\n'
            '        case "cast_pair_first_target":\n            flag("cast")\n',
            "guard",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n'
            "        case _:\n            flag(\"other\")\n",
            "`case _`",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n'
            "        case other:\n            flag(\"other\")\n",
            "`case _`",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n'
            '        case "heal":\n            flag("heal")\n',
            "does not name an imported macro",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n'
            '        case "end_turn":\n            flag("again")\n',
            "written twice",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n'
            f'        case "{NONE_FITS}":\n            flag("none")\n',
            "always offered",
        ),
        (
            'match decide("What now?"):',
            '        case "end_turn":\n            end_turn()\n',
            "at least 2 cases",
        ),
        ('match decide("What now?", **hp):', _TWO_CASES, "`**`"),
        ('match decide("What now?", scene="Battle"):', _TWO_CASES, "`scene=`"),
        ('match decide("What now?", total=hp + 1):', _TWO_CASES, "six things"),
        ('match decide("What now?", boss=find(name="Boss")):', _TWO_CASES, "six things"),
        ('match decide("What now?", note=f"{hp}"):', _TWO_CASES, "f-string"),
        ('match decide("What now?", mana=mana):', _TWO_CASES, "`mana` is not bound"),
    ],
    ids=[
        "subject not decide",
        "no question",
        "two questions",
        "empty question",
        "f-string question",
        "name question",
        "guard",
        "wildcard",
        "capture",
        "not imported",
        "duplicate",
        "none_fits",
        "one case",
        "double star",
        "reserved key",
        "arithmetic value",
        "inline find",
        "f-string value",
        "unbound value",
    ],
)
def test_a_wrong_match_is_refused(match_line: str, cases: str, said: str) -> None:
    assert said in rejection_with(decide_source(match_line, cases), battle_macros())


def test_a_wrong_context_value_names_the_six_accepted_forms() -> None:
    reason = rejection_with(
        decide_source('match decide("What now?", total=hp + 1):'), battle_macros()
    )
    for form in ("`object`", "selector(", "find_all(", "under(", "`int`", "literal"):
        assert form in reason


@pytest.mark.parametrize(
    "line",
    [
        '    decide("What now?")\n',
        '    if decide("What now?") == "end_turn":\n        end_turn()\n',
        '    if decide("What now?"):\n        end_turn()\n',
        '    choice: string = decide("What now?")\n',
    ],
    ids=["statement", "comparison", "condition", "assignment"],
)
def test_decide_outside_a_match_is_refused_and_points_at_match(line: str) -> None:
    reason = rejection_with(
        "from macros import end_turn\n\ndef m() -> None:\n" + line, battle_macros()
    )
    assert "match decide(" in reason


def test_a_match_is_refused_inside_a_predicate() -> None:
    reason = rejection_with(
        "from macros import end_turn, cast_pair_first_target\n\n"
        'def m() -> None:\n    require(ready(), "Wait.")\n\n'
        "def ready() -> bool:\n"
        '    match decide("Ready?"):\n'
        '        case "end_turn":\n            return True\n'
        '        case "cast_pair_first_target":\n            return False\n',
        battle_macros(),
    )
    assert "`-> bool` predicate allows only" in reason and "Jev" in reason


def test_a_match_counts_one_plus_its_largest_case() -> None:
    """`if` 와 같다. 가장 큰 `case` 몸통과 `match` 줄 하나."""
    imports = battle_macros()
    small = '        case "end_turn":\n            end_turn()\n'
    # `decide_source` 의 앞 두 줄(대입 둘)과 `match` 한 줄, 그리고 가장 큰 `case`.
    room = MAX_STATEMENTS - 3
    big = '        case "cast_pair_first_target":\n' + "".join(
        f'            flag("{index}")\n' for index in range(room)
    )
    parse_with(decide_source('match decide("What now?"):', small + big), imports)
    over = big + '            flag("one more")\n'
    reason = rejection_with(decide_source('match decide("What now?"):', small + over), imports)
    assert str(MAX_STATEMENTS + 1) in reason


def test_a_case_that_holds_without_releasing_is_refused() -> None:
    """어느 `case` 도 돌지 않는 경로(`NONE_FITS` 뒤에 건너뜀)가 있으므로 각 `case` 가 0 이다."""
    reason = rejection_with(
        decide_source(
            'match decide("What now?"):',
            '        case "end_turn":\n            hold_key("W")\n'
            '        case "cast_pair_first_target":\n            hold_key("W")\n',
        )
        + '    release_key("W")\n',
        battle_macros(),
    )
    assert "a held key" in reason and "same case" in reason
    parse_with(
        decide_source(
            'match decide("What now?"):',
            '        case "end_turn":\n            hold_key("W")\n            release_key("W")\n'
            '        case "cast_pair_first_target":\n            flag("cast")\n',
        ),
        battle_macros(),
    )


def test_a_match_between_a_press_and_its_release_is_refused() -> None:
    """Jev 가 `NONE_FITS` 를 고르면 `checkpoint` 처럼 멈추므로 같은 거절을 받는다."""
    source = decide_source('match decide("What now?"):').replace(
        '    match decide', '    hold_key("W")\n    match decide'
    ) + '    release_key("W")\n'
    reason = rejection_with(source, battle_macros())
    assert "a held key" in reason and "pauses" in reason


# --- `ask(...)` ---------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        '    if ask("Is the boss low?", boss=boss, hp=hp):\n        flag("low")\n',
        '    if exists(boss):\n        flag("there")\n'
        '    elif ask("Is the boss low?", boss=boss):\n        flag("low")\n',
        '    while ask("Is the battle still undecided?", boss=boss, hp=hp):\n'
        '        click(boss)\n',
        '    require(ask("Did the boss take damage?", boss=boss), "Attack again.")\n',
    ],
    ids=["if", "elif", "while", "require"],
)
def test_ask_is_a_whole_condition_of_if_elif_while_and_require(statement: str) -> None:
    definition = parse(
        "def m() -> None:\n"
        '    boss: object = find(name="Boss")\n'
        '    hp: float = member(boss, "Hp")\n' + statement
    )
    found = definition.entry.statements[2]
    if isinstance(found, MacroIfStatement) and not isinstance(
        found.condition, MacroAskCondition
    ):
        found = found.orelse[0]
    assert isinstance(found.condition, MacroAskCondition)
    assert found.condition.question.endswith("?")
    assert found.condition.context[0] == MacroContextTarget(
        key="boss", target=MacroNameTarget(name="boss")
    )


@pytest.mark.parametrize(
    "statement, said",
    [
        ('    if ask("Low?") == True:\n        flag("low")\n', "comparison"),
        ('    wait_until(ask("Low?"), 5, "Wait.")\n', "every pulse"),
        ('    ask("Low?")\n', "whole condition"),
        ('    low: bool = ask("Low?")\n', "whole condition"),
        ('    if ask("Low?", scene="Battle"):\n        flag("low")\n', "`scene=`"),
        ('    if ask(""):\n        flag("low")\n', "cannot be empty"),
        ('    if ask(f"{boss}"):\n        flag("low")\n', "f-string"),
    ],
    ids=[
        "comparison operand",
        "wait_until",
        "statement",
        "assignment",
        "reserved key",
        "empty question",
        "f-string question",
    ],
)
def test_ask_anywhere_but_a_whole_condition_is_refused(statement: str, said: str) -> None:
    reason = rejection(
        'def m() -> None:\n    boss: object = find(name="Boss")\n' + statement
    )
    assert said in reason


@pytest.mark.parametrize(
    "body",
    [
        '    if ask("Ready?"):\n        return True\n    return False\n',
        '    return ask("Ready?")\n',
    ],
    ids=["if", "return"],
)
def test_ask_is_refused_inside_a_predicate(body: str) -> None:
    reason = rejection(
        'def m() -> None:\n    require(ready(), "Wait.")\n\n'
        "def ready() -> bool:\n" + body
    )
    assert "synchronously" in reason


# --- `under(...)`: object 하나와 그 아래 전부 -------------------------------------


def test_under_takes_a_path_or_an_object_name() -> None:
    from app.agents.qa.macro.model import MacroContextUnder

    definition = parse_with(
        decide_source(
            'match decide("What now?", hand=under("/Root[0]/Hand[2]/"), around=under(boss)):'
        ),
        battle_macros(),
    )
    match = definition.entry.statements[2]
    context = {argument.key: argument for argument in match.decide.context}

    assert context["hand"] == MacroContextUnder(
        key="hand", root=MacroSelectorTarget(selector="Root[0]/Hand[2]")
    )
    assert context["around"] == MacroContextUnder(
        key="around", root=MacroNameTarget(name="boss")
    )


def test_under_works_in_ask_too() -> None:
    source = (
        "def m() -> None:\n"
        '    if ask("Is the hand empty?", hand=under("Root[0]/Hand[2]")):\n'
        '        flag("empty")\n'
    )
    definition = macro_definition_from_source("m", source)
    condition = definition.entry.statements[0].condition

    assert condition.context[0].kind == "under"


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("under()", "takes one thing"),
        ('under("A", "B")', "takes one thing"),
        ('under(path="A")', "takes one thing"),
        ('under("  /  ")', "takes one thing"),
        ("under(3)", "takes one thing"),
        ("under(hp)", "declared `float`, not `object`"),
        ("under(mana)", "`mana` is not bound"),
        ('under(selector("A"))', "takes one thing"),
    ],
    ids=["nothing", "two", "keyword", "empty path", "number", "reading", "unbound", "selector call"],
)
def test_under_refuses_anything_but_one_path_or_object(argument: str, expected: str) -> None:
    reason = rejection_with(
        decide_source(f'match decide("What now?", hand={argument}):'), battle_macros()
    )

    assert expected in reason
