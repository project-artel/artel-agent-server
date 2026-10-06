"""`find` 와 `selector` 가 `PulseMemory` 하나로 풀리는가, 그리고 비교가 평가되는가.

핵심 반전이 하나다. `SendsGameState` 의 기본값이 꺼짐이라 `SceneMemory.observables` 는
기본 빌드에서 한 번도 안 찬다. 그래서 아래 memory 는 전부 `PULSE` 프레임만으로 만든다 —
`GAME_STATE` 없이 도는가가 이 작업의 전부다.

`(값의 모양, 연산자)` 표 마흔두 칸의 기대는 `tests/test_qa_macro_operators.py` 가 든다.
이 파일은 그 기대를 **평가 시점**에 돌린다.
"""

import pytest

from app.agents.qa.macro.binding import (
    FoundObject,
    LateSelector,
    MacroMemories,
    MacroScope,
    compare,
    evaluate,
    read,
    resolve_find,
    resolve_target,
    shape_of,
)
from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    COMPARISON_REJECTED,
    SELECTOR_AMBIGUOUS,
    SELECTOR_NOT_FOUND,
    STALE_BINDING,
    MacroFailure,
)
from app.agents.qa.macro.grammar import MacroOperator, MacroShape
from app.agents.qa.macro.model import (
    MacroComparisonCondition,
    MacroFindValue,
    MacroLiteral,
    MacroLiteralOperand,
    MacroNameOperand,
    MacroNameTarget,
    MacroReaderCall,
    MacroReaderCondition,
    MacroSelectorTarget,
    MacroStringLiteral,
    MacroType,
)
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory
from tests.test_qa_macro_operators import CELLS, EXPECTED


def memories(*objects: dict, statics: list[dict] | None = None, scene: str = "Battle"):
    """`PULSE` 프레임만으로 memory 를 만든다. `GAME_STATE` 는 한 장도 안 쓴다."""
    memory = SceneMemory()
    memory.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": scene,
                "whole": True,
                "active": list(objects),
                "statics": statics or [],
            }
        )
    )
    return MacroMemories(scene=memory)


def card(
    selector: str,
    text: str | None = None,
    instance_id: int | None = 1,
    offers: dict | None = None,
    members: list[dict] | None = None,
) -> dict:
    return {
        "selector": selector,
        "id": instance_id,
        "text": text,
        "offers": offers,
        "members": members or [],
    }


def find(**keywords) -> MacroFindValue:
    return MacroFindValue(
        **{
            keyword: MacroStringLiteral(value=value)
            for keyword, value in keywords.items()
        }
    )


# --- `find` 의 네 갈림 ---------------------------------------------------------


def test_a_unique_label_resolves_to_the_record_rather_than_to_an_id() -> None:
    """묶인 이름이 드는 값은 id 하나가 아니라 기록 자체다.

    그 기록이 `id` 와 `selector` 를 둘 다 들고 있어, 뒤이은 action 은 어느 쪽으로도
    겨눌 수 있다.
    """
    held = memories(
        card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot", instance_id=41),
        card("Root[0]/Hand[2]/Card(Clone)[4]", text="fire", instance_id=42),
    )

    found = resolve_find(held, MacroScope(), find(label="shoot"))

    assert found.record.id == 41
    assert found.record.selector == "Root[0]/Hand[2]/Card(Clone)[3]"
    assert found.key == "Battle/Root[0]/Hand[2]/Card(Clone)[3]"


def test_two_cards_showing_the_same_text_are_ambiguous() -> None:
    """서버가 임의로 하나를 고르면 그것은 서버가 지어낸 것이 된다."""
    held = memories(
        card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot"),
        card("Root[0]/Hand[2]/Card(Clone)[4]", text="shoot"),
    )

    with pytest.raises(MacroFailure) as failed:
        resolve_find(held, MacroScope(), find(label="shoot"))

    assert failed.value.code == SELECTOR_AMBIGUOUS
    assert len(failed.value.payload["matched"]) == 2


def test_two_cards_with_the_same_name_but_different_text_are_told_apart_by_label() -> None:
    """구멍이 좁아진 것이지 없어진 것은 아니다.

    이름(selector 마지막 세그먼트)이 같아도 글자가 다르면 `label=` 조회는 갈린다.
    """
    held = memories(
        card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot"),
        card("Root[0]/Hand[2]/Card(Clone)[4]", text="fire"),
    )

    assert resolve_find(held, MacroScope(), find(label="fire")).record.selector.endswith("[4]")
    # 같은 두 장을 `name=` 으로 물으면 갈리지 않는다.
    with pytest.raises(MacroFailure) as failed:
        resolve_find(held, MacroScope(), find(name="Card(Clone)"))
    assert failed.value.code == SELECTOR_AMBIGUOUS


def test_nothing_matching_says_to_observe_before_blaming_the_macro() -> None:
    """대상의 첫 판독이 아직 안 왔을 수 있다. 그 쪽이 더 흔하다."""
    held = memories(card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot"))

    with pytest.raises(MacroFailure) as failed:
        resolve_find(held, MacroScope(), find(label="reload"))

    assert failed.value.code == SELECTOR_NOT_FOUND
    assert "Observe" in failed.value.reason


def test_a_bound_record_that_is_gone_is_stale_and_not_not_found() -> None:
    """앞엣것은 다시 부르면 되고 뒤엣것은 정의를 고쳐야 한다. 섞으면 할 일이 뒤바뀐다."""
    held = memories(card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot"))
    found = resolve_find(held, MacroScope(), find(label="shoot"))
    scope = MacroScope({"card": found})

    # 그 카드가 파괴된다. `gone` 이 memory 에서 그 기록을 지운다.
    held.pulse.apply(
        PulseReading.model_validate(
            {"scene": "Battle", "gone": ["Battle/Root[0]/Hand[2]/Card(Clone)[3]"]}
        )
    )

    with pytest.raises(MacroFailure) as failed:
        resolve_target(held, scope, MacroNameTarget(name="card"))

    assert failed.value.code == STALE_BINDING
    assert failed.value.code != SELECTOR_NOT_FOUND


def test_a_run_with_no_pulse_at_all_is_refused_with_one_code() -> None:
    """읽을 자리가 아예 없을 때 하나만 이 코드로 남는다."""
    empty = MacroMemories(scene=SceneMemory())

    for call in (
        lambda: resolve_find(empty, MacroScope(), find(label="shoot")),
        lambda: resolve_target(
            empty, MacroScope(), MacroSelectorTarget(selector="Root[0]")
        ),
    ):
        with pytest.raises(MacroFailure) as failed:
            call()
        assert failed.value.code == ACTION_REJECTED


def test_under_narrows_by_prefix_with_or_without_the_scene_name() -> None:
    """key 는 씬 이름 뒤에 selector 를 붙인 전체 경로다.

    agent 가 화면에서 읽는 주소에는 씬 이름이 안 붙어 있으므로 둘 다 받는다.
    """
    held = memories(
        card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot"),
        card("Root[0]/Deck[5]/Card(Clone)[6]", text="shoot"),
    )

    for under in ("Root[0]/Hand[2]", "Battle/Root[0]/Hand[2]"):
        found = resolve_find(held, MacroScope(), find(label="shoot", under=under))
        assert found.record.selector == "Root[0]/Hand[2]/Card(Clone)[3]"


def test_name_matches_the_last_segment_with_or_without_its_index() -> None:
    held = memories(card("Root[0]/Canvas[1]/Combine[2]", text=None))

    for name in ("Combine", "Combine[2]", "Root[0]/Canvas[1]/Combine[2]"):
        assert resolve_find(held, MacroScope(), find(name=name)).record.selector == (
            "Root[0]/Canvas[1]/Combine[2]"
        )
    with pytest.raises(MacroFailure) as failed:
        resolve_find(held, MacroScope(), find(name="Combin"))
    assert failed.value.code == SELECTOR_NOT_FOUND


def test_a_selector_is_resolved_where_it_is_used_rather_than_where_it_was_bound() -> None:
    """`selector(...)` 를 묶은 이름은 그 자리에서 풀지 않는다.

    즉시 풀면 적어 둔 주소인데도 `STALE_BINDING` 이 나서, 적어 둔 주소면
    `REQUIRE_FAILED`, 이 런에서 찾은 값이면 `STALE_BINDING` 이라는 기준이 깨진다.
    """
    held = memories(card("Root[0]/Canvas[1]/Combine[2]"))
    scope = MacroScope({"zone": LateSelector(selector="Root[0]/Canvas[1]/Combine[2]")})

    assert resolve_target(held, scope, MacroNameTarget(name="zone")).record.id == 1

    held.pulse.apply(
        PulseReading.model_validate(
            {"scene": "Battle", "gone": ["Battle/Root[0]/Canvas[1]/Combine[2]"]}
        )
    )
    with pytest.raises(MacroFailure) as failed:
        resolve_target(held, scope, MacroNameTarget(name="zone"))
    assert failed.value.code == SELECTOR_NOT_FOUND


# --- 읽는 호출 여덟 -----------------------------------------------------------


def reader(name: str, target=None, key: str | None = None) -> MacroReaderCall:
    return MacroReaderCall(
        reader=name,
        target=target,
        key=None if key is None else MacroStringLiteral(value=key),
    )


def test_text_reads_the_letters_the_object_is_showing() -> None:
    """라벨이 무엇으로 바뀌었는지 물을 수 없게 되는 것은 QA 에서 비용이 너무 크다."""
    held = memories(card("Root[0]/Hp[1]", text="42"))

    assert read(held, MacroScope(), reader("text", MacroSelectorTarget(selector="Root[0]/Hp[1]"))) == "42"


def test_actionable_answers_from_offers_alone() -> None:
    """`id` 는 거의 모든 객체에 실리므로 그것으로 가르면 아무것도 안 걸러진다."""
    held = memories(
        card("Root[0]/Go[1]", offers={"clicks": ["onClick"]}),
        card("Root[0]/Label[2]", text="hello", offers=None),
    )
    scope = MacroScope()

    assert read(held, scope, reader("actionable", MacroSelectorTarget(selector="Root[0]/Go[1]"))) is True
    assert read(held, scope, reader("actionable", MacroSelectorTarget(selector="Root[0]/Label[2]"))) is False


def test_exists_and_absent_answer_without_stopping_the_macro() -> None:
    held = memories(card("Root[0]/Go[1]"))
    scope = MacroScope()
    there = MacroSelectorTarget(selector="Root[0]/Go[1]")
    gone = MacroSelectorTarget(selector="Root[0]/Nowhere[9]")

    assert read(held, scope, reader("exists", there)) is True
    assert read(held, scope, reader("absent", there)) is False
    assert read(held, scope, reader("exists", gone)) is False
    assert read(held, scope, reader("absent", gone)) is True


def test_scene_reads_the_name_from_pulse_when_game_state_never_arrives() -> None:
    held = memories(card("Root[0]/Go[1]"), scene="Battle")

    assert held.scene.scene is None
    assert read(held, MacroScope(), reader("scene")) == "Battle"


def test_member_reads_a_value_the_game_watches_on_that_object() -> None:
    """기본 빌드에서 수를 읽을 자리가 이것뿐이다."""
    held = memories(
        card(
            "Root[0]/Enemy[1]",
            members=[{"on": "Game.Enemy", "member": "Hp", "value": 42}],
        )
    )
    scope = MacroScope()
    target = MacroSelectorTarget(selector="Root[0]/Enemy[1]")

    assert read(held, scope, reader("member", target, "Enemy.Hp")) == 42
    with pytest.raises(MacroFailure) as failed:
        read(held, scope, reader("member", target, "Enemy.Shield"))
    assert failed.value.code == ACTION_REJECTED
    assert "Enemy.Hp" in failed.value.payload["available"]


def test_static_reads_a_latching_flag_by_the_name_the_view_prints() -> None:
    held = memories(
        card("Root[0]/Go[1]"),
        statics=[
            {"declaring": "Game.InteractionLock", "member": "IsLocked", "value": True}
        ],
    )

    assert read(held, MacroScope(), reader("static", key="InteractionLock.IsLocked")) is True
    with pytest.raises(MacroFailure) as failed:
        read(held, MacroScope(), reader("static", key="Nothing.Here"))
    assert failed.value.code == ACTION_REJECTED


def test_observable_says_why_it_answers_nothing_on_a_default_build() -> None:
    """`GAME_STATE` 가 기본으로 꺼져 있다. 거절 문장이 `static()` 을 대 줘야 한다."""
    held = memories(card("Root[0]/Go[1]"))

    with pytest.raises(MacroFailure) as failed:
        read(held, MacroScope(), reader("observable", key="Score"))

    assert failed.value.code == ACTION_REJECTED
    assert "static(" in failed.value.reason


# --- 도착한 값의 모양 ----------------------------------------------------------


@pytest.mark.parametrize(
    "value, shape",
    [
        (True, MacroShape.bool_),
        (False, MacroShape.bool_),
        # bool 을 먼저 가린다. Python 에서 `True` 는 int 이기도 하다.
        (1, MacroShape.number),
        (3.0, MacroShape.number),
        (-2, MacroShape.number),
        ("shoot", MacroShape.string),
        ("", MacroShape.string),
        ({"x": 1.0, "y": 2.0}, MacroShape.vector2),
        ({"x": 1.0, "y": 2.0, "z": 3.0}, MacroShape.vector3),
        ({"path": "Root[0]/Go[1]", "world": {"x": 0.0}}, MacroShape.object_),
        ({"unread": "not-a-number"}, MacroShape.other),
        ({"sprite": "card_back"}, MacroShape.other),
        (None, MacroShape.other),
        (LateSelector(selector="Root[0]"), MacroShape.object_),
    ],
)
def test_the_shape_of_an_arrived_value(value, shape: MacroShape) -> None:
    assert shape_of(value) is shape


# --- 표 마흔두 칸을 평가 시점에 -------------------------------------------------

_HELD = memories(
    card("Root[0]/A[1]", instance_id=None),
    card("Root[0]/B[2]", instance_id=None),
)

# 각 모양으로 도착하는 값 한 쌍. 양쪽이 같은 모양이라, 모양이 달라서 나는 거절과 섞이지
# 않는다.
_ARRIVED: dict[MacroShape, tuple[object, object]] = {
    MacroShape.number: (5, 5),
    MacroShape.string: ("a", "a"),
    MacroShape.bool_: (True, True),
    MacroShape.vector2: ({"x": 1.0, "y": 2.0}, {"x": 1.0, "y": 2.0}),
    MacroShape.vector3: (
        {"x": 1.0, "y": 2.0, "z": 3.0},
        {"x": 1.0, "y": 2.0, "z": 3.0},
    ),
    MacroShape.object_: (
        LateSelector(selector="Root[0]/A[1]"),
        LateSelector(selector="Root[0]/A[1]"),
    ),
    MacroShape.other: ({"unread": "not-a-number"}, {"unread": "not-a-number"}),
}


@pytest.mark.parametrize("shape, operator", CELLS)
def test_every_cell_of_the_table_is_enforced_on_the_value_that_arrives(
    shape: MacroShape, operator: MacroOperator
) -> None:
    left, right = _ARRIVED[shape]

    if EXPECTED[(shape, operator)]:
        # 되는 칸은 비교가 돈다. 값이 같은 쌍이므로 `==`·`>=`·`<=` 는 참, 나머지는 거짓.
        assert compare(_HELD, left, operator, right) is (
            operator
            in (
                MacroOperator.equal,
                MacroOperator.greater_or_equal,
                MacroOperator.less_or_equal,
            )
        )
        return

    with pytest.raises(MacroFailure) as failed:
        compare(_HELD, left, operator, right)

    assert failed.value.code == COMPARISON_REJECTED
    # payload 가 도착한 값의 모양, 건 연산자, 그 모양에서 되는 연산자를 이름으로 댄다.
    assert failed.value.payload["shape"] == shape.value
    assert failed.value.payload["operator"] == operator.value
    assert failed.value.payload["allowed"] == [
        one.value for one in MacroOperator if EXPECTED[(shape, one)]
    ]


def test_a_number_equality_goes_through_rather_than_being_refused() -> None:
    """`speed == 1.5` 가 통과한다. 위태롭다는 것은 권고이지 거절이 아니다."""
    assert compare(_HELD, 1.5, MacroOperator.equal, 1.5) is True
    assert compare(_HELD, 1.5, MacroOperator.equal, 2.0) is False


def test_an_unread_value_is_refused_rather_than_quietly_false() -> None:
    """`require` 가 게임 결함처럼 실패하는 것을 막는다."""
    for operator in (MacroOperator.equal, MacroOperator.less):
        with pytest.raises(MacroFailure) as failed:
            compare(_HELD, {"unread": "not-a-number"}, operator, 5)
        assert failed.value.code == COMPARISON_REJECTED


def test_two_sides_of_different_shapes_are_refused_with_both_named() -> None:
    with pytest.raises(MacroFailure) as failed:
        compare(_HELD, "0", MacroOperator.equal, 0)

    assert failed.value.code == COMPARISON_REJECTED
    assert failed.value.payload["left_shape"] == "string"
    assert failed.value.payload["right_shape"] == "number"


def test_an_int_and_a_float_are_one_shape_and_compare() -> None:
    """wire 가 네 자리로 반올림해 `3.0` 을 `3` 으로 보낸다. 값만 보고는 가릴 수 없다."""
    assert compare(_HELD, 3, MacroOperator.equal, 3.0) is True


def test_two_objects_are_told_apart_by_pulse_key_even_with_no_id() -> None:
    """`PulseObject.id` 가 `int | None` 이라 `id` 로 가르면 `None` 인 기록에 규칙이 없다."""
    first = FoundObject(key="Battle/Root[0]/A[1]", record=_HELD.pulse.held["Battle/Root[0]/A[1]"])
    second = FoundObject(key="Battle/Root[0]/B[2]", record=_HELD.pulse.held["Battle/Root[0]/B[2]"])

    assert first.record.id is None and second.record.id is None
    assert compare(_HELD, first, MacroOperator.equal, second) is False
    assert compare(_HELD, first, MacroOperator.not_equal, second) is True
    assert compare(_HELD, first, MacroOperator.equal, first) is True
    # `selector(...)` 와 견주는 것도 같은 키로 가른다.
    assert compare(_HELD, first, MacroOperator.equal, LateSelector(selector="Root[0]/A[1]")) is True


# --- 조건 하나 ----------------------------------------------------------------


def test_a_comparison_condition_is_evaluated_through_the_table() -> None:
    held = memories(card("Root[0]/Hp[1]", text="42"))
    condition = MacroComparisonCondition(
        left={"kind": "reader", "call": reader("text", MacroSelectorTarget(selector="Root[0]/Hp[1]"))},
        operator=MacroOperator.equal,
        right=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.string, value="42")),
    )

    assert evaluate(held, MacroScope(), condition) is True


def test_a_condition_with_no_comparison_has_to_arrive_as_a_bool() -> None:
    """`static()`·`observable()`·`member()` 셋만 여기 닿는다. 파서가 나머지를 거절한다.

    그 밖의 모양을 진리값으로 접으면 빈 문자열이 거짓, 아무 사전이 참이 되어 조용히
    틀린다 — `require` 가 게임 결함처럼 실패하는 바로 그 경우다.
    """
    held = memories(
        card("Root[0]/Go[1]"),
        statics=[{"declaring": "Game.Lock", "member": "IsLocked", "value": True}],
    )
    locked = MacroReaderCondition(call=reader("static", key="Lock.IsLocked"))

    assert evaluate(held, MacroScope(), locked) is True

    counted = memories(
        card("Root[0]/Go[1]"),
        statics=[{"declaring": "Game.Lock", "member": "IsLocked", "value": 3}],
    )
    with pytest.raises(MacroFailure) as failed:
        evaluate(counted, MacroScope(), locked)
    assert failed.value.code == COMPARISON_REJECTED
    assert failed.value.payload["shape"] == "number"


def test_an_unknown_shape_in_an_if_condition_is_refused_the_same_way() -> None:
    """평가기가 하나다. 거절된 `if` 조건을 거짓으로 읽어 분기를 조용히 넘기면 안 된다."""
    held = memories(
        card("Root[0]/Go[1]"),
        statics=[{"declaring": "Game.X", "member": "y", "value": {"unread": "not-a-number"}}],
    )
    condition = MacroComparisonCondition(
        left={"kind": "reader", "call": reader("static", key="X.y")},
        operator=MacroOperator.less,
        right=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.number, value=5)),
    )

    with pytest.raises(MacroFailure) as failed:
        evaluate(held, MacroScope(), condition)

    assert failed.value.code == COMPARISON_REJECTED


def test_a_bound_literal_name_keeps_its_value_in_a_comparison() -> None:
    held = memories(card("Root[0]/Go[1]"))
    scope = MacroScope({"speed": 1.5})
    condition = MacroComparisonCondition(
        left=MacroNameOperand(name="speed", declared_type=MacroType.float_),
        operator=MacroOperator.greater,
        right=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.number, value=1)),
    )

    assert evaluate(held, scope, condition) is True
