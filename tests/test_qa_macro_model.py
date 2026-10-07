"""통과한 macro tree 의 저장 표현.

재는 것이 둘이다. 하나는 JSON 이 선언 타입까지 들고 돌아오는가 — 안 들면 `run_macro`
의 인자 검사가 설 자리가 없다. 다른 하나는 **텍스트가 원본이고 JSON 이 파생이라는
관계가 코드로 못박혀 있는가** 다. JSON 만 고치는 경로가 하나라도 있으면, 정의가 바뀌었을
때 사람이 보는 diff 가 실제로 바뀐 줄을 가리키지 않게 된다.
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.qa.macro.grammar import MacroType
from app.agents.qa.macro.model import (
    MacroAssignStatement,
    MacroDefinition,
    MacroIfStatement,
)
from app.agents.qa.macro.parser import macro_definition_from_source

EXAMPLE = '''
def attack_with_combined_card(card_a: object, card_b: object, button: int = 0) -> None:
    require(scene() == "Battle", "Open the battle screen before calling this.")
    zone: object = find(name="CombineZone")
    enemy: object = find(label="Enemy")
    drag(card_a, zone, button)
    drag(card_b, zone, button)
    require(
        actionable(selector("Root[0]/Canvas[1]/Combine[2]")),
        "Combine both cards before attacking.",
    )
    click(selector("Root[0]/Canvas[1]/Combine[2]"))
    click(selector("Root[0]/Canvas[1]/Attack[3]"))
    require(text(enemy) != "100", "Attack again; the enemy took no damage.")
'''

WITH_HELPERS = '''
def attack(card: object, speed: float = 1.5) -> None:
    aim(card)
    if actionable(card):
        strike(speed)
        flag("the card was pressable")
    else:
        ask_verdict("a card that cannot be pressed should say why")

def aim(card: object) -> None:
    move_pointer(card)

def strike(speed: float) -> None:
    press_key("Space", speed)
'''


def round_trip(definition: MacroDefinition) -> MacroDefinition:
    """직렬화하고 다시 읽는다. JSON 이 실제로 실린 것만 들고 돌아오는지 보는 자리다."""
    return MacroDefinition.model_validate_json(definition.model_dump_json())


@pytest.mark.parametrize(
    "name, source",
    [("attack_with_combined_card", EXAMPLE), ("attack", WITH_HELPERS)],
)
def test_a_definition_survives_a_round_trip_unchanged(name: str, source: str) -> None:
    definition = macro_definition_from_source(name, source)

    assert round_trip(definition) == definition


def test_the_source_text_rides_along_verbatim() -> None:
    """`ast.parse` 가 주석과 공백을 버리므로, JSON 에서 텍스트를 다시 찍어낼 수 없다.

    찍어내면 agent 가 적은 그대로가 아니고, 정의가 바뀌었을 때 diff 가 실제로 바뀐 줄을
    가리키지 않는다.
    """
    commented = (
        "def m(card: object) -> None:\n"
        "    # 카드가 눌릴 때까지 기다린다\n"
        '    require(actionable(card), "wait for the card to settle")\n'
        "\n"
        "    click(card)\n"
    )
    definition = macro_definition_from_source("m", commented)

    assert definition.source == commented
    assert round_trip(definition).source == commented


def test_the_declared_types_are_in_the_json_tree_and_survive_it() -> None:
    definition = macro_definition_from_source("attack_with_combined_card", EXAMPLE)
    written = json.loads(definition.model_dump_json())

    assert [
        (parameter["name"], parameter["declared_type"])
        for parameter in written["entry"]["parameters"]
    ] == [("card_a", "object"), ("card_b", "object"), ("button", "int")]
    assert written["entry"]["parameters"][2]["default"] == {
        "shape": "number",
        "value": 0,
    }

    assignments = [
        statement
        for statement in written["entry"]["statements"]
        if statement["kind"] == "assign"
    ]
    assert [(one["name"], one["declared_type"]) for one in assignments] == [
        ("zone", "object"),
        ("enemy", "object"),
    ]

    back = round_trip(definition)
    assert back.entry.parameters[2].declared_type is MacroType.int_
    assert back.entry.parameters[2].default.value == 0
    stored = [
        statement
        for statement in back.entry.statements
        if isinstance(statement, MacroAssignStatement)
    ]
    assert [one.declared_type for one in stored] == [
        MacroType.object_,
        MacroType.object_,
    ]


def test_the_entry_point_and_the_helpers_stay_apart_through_the_round_trip() -> None:
    definition = round_trip(macro_definition_from_source("attack", WITH_HELPERS))

    assert definition.entry.name == "attack"
    assert sorted(helper.name for helper in definition.helpers) == [
        "aim",
        "strike",
    ]
    assert definition.function("aim") is definition.helpers[
        [helper.name for helper in definition.helpers].index("aim")
    ]
    assert definition.function("nobody") is None


def test_an_if_keeps_its_condition_and_both_bodies() -> None:
    definition = round_trip(macro_definition_from_source("attack", WITH_HELPERS))
    branch = definition.entry.statements[1]

    assert isinstance(branch, MacroIfStatement)
    assert branch.condition.kind == "call"
    assert branch.condition.call.reader == "actionable"
    assert [statement.kind for statement in branch.body] == ["helper", "flag"]
    assert [statement.kind for statement in branch.orelse] == ["ask_verdict"]


def test_every_statement_carries_the_line_as_the_author_wrote_it() -> None:
    """`ctx.run` 의 `summary` 에 들어가는 값이고, **원문 그대로**여야 한다.

    `ast.unparse` 로 되찍지 않는다. 그것은 따옴표를 바꾸고 주석을 버리므로 timeline 에
    남는 것이 agent 가 쓴 글자가 아니게 된다 — 텍스트가 원본이고 JSON 이 파생이라는
    원칙이 바로 그 자리에서 깨진다.
    """
    definition = round_trip(macro_definition_from_source("attack", WITH_HELPERS))

    assert definition.entry.statements[0].source == "aim(card)"
    # `if` 는 조건 줄만 든다. 몸통까지 들면 한 statement 의 텍스트가 블록 전체가 되는데,
    # 이 값이 쓰이는 자리는 `ACTION` frame 의 한 줄이다.
    assert definition.entry.statements[1].source == "if actionable(card):"
    assert definition.entry.statements[1].body[0].source == "strike(speed)"


def test_the_stored_line_keeps_the_quotes_and_the_comment_the_author_wrote() -> None:
    """`ast.unparse` 가 바꿔 버리는 것이 정확히 이 둘이다.

    따옴표(`"CardSlot/0"` 이 `'CardSlot/0'` 이 된다)와 주석. 둘 중 하나라도 바뀌면
    macro 를 쓴 agent 가 timeline 에서 자기가 쓴 줄을 못 알아보고, 그것이 디버깅할 때
    제일 비싼 종류의 혼선이다.
    """
    written = (
        "def m(card: object) -> None:\n"
        "    # 슬롯으로 끌어 놓는다\n"
        '    slot: object = selector("CardSlot/0")   # 첫 슬롯\n'
        "    drag(\n"
        "        card,\n"
        "        slot,\n"
        "    )\n"
    )
    definition = round_trip(macro_definition_from_source("m", written))

    assert definition.entry.statements[0].source == (
        'slot: object = selector("CardSlot/0")   # 첫 슬롯'
    )
    # 여러 줄에 걸친 호출은 걸친 줄 전부를 그대로 든다. 안쪽 정렬이 남는다.
    assert definition.entry.statements[1].source == "drag(\n    card,\n    slot,\n)"
    # 그리고 어느 statement 에도 `ast.unparse` 가 쓰는 작은따옴표가 없다.
    assert all(
        "'" not in statement.source for statement in definition.entry.statements
    )


# --- 텍스트가 원본이라는 관계 ---------------------------------------------------


def test_a_require_node_keeps_the_two_key_names_a_db_check_matches_on() -> None:
    """`artel-orchestration-server` 의 CHECK 제약 하나가 이 두 이름에 기댄다.

    `ck_macro_require_carries_remedy`(`V98__store_registered_macros_on_the_content_map.sql`)
    가 `jsonb_path_exists` 로 `$.**` 를 훑어, `kind` 가 `"require"` 인데 `remedy` 가
    없거나 비거나 문자열이 아닌 node 가 있으면 행을 거절한다. `require` 가 `if` 몸통
    안에 중첩될 수 있어 고정 경로로는 닿지 않으므로 tree 전체를 훑는다.

    **둘 중 하나라도 이름이 바뀌면 그 제약이 조용히 아무것도 막지 않게 된다.**
    `NOT jsonb_path_exists(...)` 가 어떤 tree 에도 참이 되고, 에러는 안 나고 검증만
    사라진다. 그래서 여기서 이름을 못박는다 — 저쪽 repository 의 테스트는 이 PR 의
    diff 에서 안 보인다.

    목록이 든 key 이름(`statements`·`body`·`orelse`)에는 기대지 않으므로 그쪽은
    자유롭다.
    """
    nested = json.loads(
        macro_definition_from_source(
            "m",
            "def m(card: object) -> None:\n"
            "    if exists(card):\n"
            '        require(actionable(card), "wait for the card to settle")\n',
        ).model_dump_json()
    )["entry"]["statements"][0]["body"][0]

    assert nested["kind"] == "require"
    assert isinstance(nested["remedy"], str)
    assert nested["remedy"] == "wait for the card to settle"


def test_a_stored_definition_cannot_be_edited_in_place() -> None:
    """고치는 유일한 길은 원본 텍스트를 다시 파싱하는 것이다."""
    definition = macro_definition_from_source("attack_with_combined_card", EXAMPLE)

    with pytest.raises(ValidationError):
        definition.name = "something_else"
    with pytest.raises(ValidationError):
        definition.source = "def other() -> None:\n    pause_game_time()\n"
    with pytest.raises(ValidationError):
        definition.entry.statements[0].remedy = "do nothing"
    # 순서 있는 것은 전부 `tuple` 이다. `list` 면 frozen 모델 안에서도 늘어난다.
    assert isinstance(definition.entry.statements, tuple)
    assert isinstance(definition.helpers, tuple)
    with pytest.raises(AttributeError):
        definition.entry.statements.append(definition.entry.statements[0])


def test_a_json_tree_with_an_extra_key_is_refused() -> None:
    """손으로 키를 더한 정의가 조용히 통과하면 텍스트가 원본이라는 말이 거짓이 된다."""
    definition = macro_definition_from_source("attack_with_combined_card", EXAMPLE)
    written = json.loads(definition.model_dump_json())
    written["entry"]["statements"][0]["invented"] = "yes"

    with pytest.raises(ValidationError):
        MacroDefinition.model_validate(written)


def test_changing_the_source_text_is_what_changes_the_definition() -> None:
    """같은 이름, 다른 텍스트 → 다른 정의. 그것이 고치는 길의 전부다."""
    before = macro_definition_from_source("m", 'def m(card: object) -> None:\n    click(card)\n')
    after = macro_definition_from_source(
        "m", "def m(card: object) -> None:\n    double_click(card)\n"
    )

    assert before != after
    assert before.entry.statements[0].callee == "click"
    assert after.entry.statements[0].callee == "double_click"


def test_the_parser_is_the_only_place_a_definition_is_built() -> None:
    """`MacroDefinition` 을 만드는 호출이 코드에 하나뿐이다.

    `model_validate` 로 만드는 길이 어딘가에 열리면, 그 길로 들어온 정의는 어떤 텍스트
    에서도 나오지 않은 것이 된다. 테스트의 `round_trip` 은 그 길을 **일부러** 쓰는
    자리라 여기서 세지 않는다.
    """
    from pathlib import Path

    application = Path(__file__).resolve().parent.parent / "app"
    built: list[str] = []
    for path in sorted(application.rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            # 선언하는 줄은 세지 않는다. 세는 것은 부르는 자리다.
            if line.lstrip().startswith("class "):
                continue
            if "MacroDefinition(" in line or "MacroDefinition.model_validate" in line:
                built.append(f"{path.relative_to(application)}:{number}")

    assert len(built) == 1, built
    assert built[0].startswith("agents/qa/macro/parser.py:")
