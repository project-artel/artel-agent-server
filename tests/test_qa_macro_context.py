"""`decide(...)` 와 `ask(...)` 의 keyword 인자가 Jev `state` 로 어떻게 풀리는가."""

import json

import pytest

from app.agents.qa.macro.binding import MacroMemories, MacroScope, resolve_find
from app.agents.qa.macro.context import build_context_state
from app.agents.qa.macro.errors import SELECTOR_NOT_FOUND, MacroFailure
from app.agents.qa.macro.grammar import (
    MAX_CONTEXT_VALUE_CHARS,
    MAX_DECIDE_STATE_CHARS,
    MacroShape,
    MacroType,
)
from app.agents.qa.macro.model import (
    MacroContextFindAll,
    MacroContextLiteral,
    MacroContextName,
    MacroContextTarget,
    MacroContextUnder,
    MacroFindValue,
    MacroLiteral,
    MacroNameTarget,
    MacroSelectorTarget,
    MacroStringLiteral,
)
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory

BOSS = "Root[0]/Boss_Undead(Clone)[4]"


def member(on: str, name: str, value) -> dict:
    return {"on": on, "member": name, "value": value}


def thing(selector: str, text: str | None = None, members: list[dict] | None = None) -> dict:
    return {"selector": selector, "id": 1, "text": text, "members": members or []}


def memories(*objects: dict, scene: str = "TurnBattleScene") -> MacroMemories:
    memory = SceneMemory()
    memory.pulse.apply(
        PulseReading.model_validate({"scene": scene, "whole": True, "active": list(objects)})
    )
    return MacroMemories(scene=memory)


def battle() -> MacroMemories:
    return memories(
        thing(
            BOSS,
            text="Undead",
            members=[
                member("Game.Enemy", "Hp", 40),
                member("Game.Enemy", "Shield", 5),
                member("Game.Wobble", "Phase", 0.25),
            ],
        ),
        thing("Root[0]/Hand[2]/Card(Clone)[1]", text="Fire"),
        thing("Root[0]/Hand[2]/Card(Clone)[2]", text="Shoot"),
    )


def boss_target() -> MacroContextTarget:
    return MacroContextTarget(key="boss", target=MacroSelectorTarget(selector=BOSS))


def test_a_target_renders_its_path_text_and_members_with_scene_always_present() -> None:
    state, clipped = build_context_state(battle(), MacroScope(), (boss_target(),))

    assert clipped == 0
    assert state == {
        "scene": "TurnBattleScene",
        "boss": {
            "path": BOSS,
            "text": "Undead",
            "members": {"Enemy.Hp": 40, "Enemy.Shield": 5, "Wobble.Phase": 0.25},
        },
    }
    assert list(state) == ["scene", "boss"]


def test_a_member_judged_cosmetic_is_left_out() -> None:
    reading = battle()
    reading.pulse.relevance["Game.Wobble::Phase"] = 0.1
    reading.pulse.relevance["Game.Enemy::Hp"] = 0.9

    state, _clipped = build_context_state(reading, MacroScope(), (boss_target(),))

    assert state["boss"]["members"] == {"Enemy.Hp": 40, "Enemy.Shield": 5}


def test_an_object_without_text_has_no_text_key() -> None:
    reading = memories(thing("Root[0]/Plain[1]"))
    target = MacroContextTarget(key="plain", target=MacroSelectorTarget(selector="Root[0]/Plain[1]"))

    state, _clipped = build_context_state(reading, MacroScope(), (target,))

    assert state["plain"] == {"path": "Root[0]/Plain[1]", "members": {}}


def test_a_name_bound_to_an_object_renders_the_object_as_it_is_now() -> None:
    reading = battle()
    scope = MacroScope()
    scope.bind(
        "boss",
        resolve_find(reading, scope, MacroFindValue(label=MacroStringLiteral(value="Undead"))),
    )
    target = MacroContextTarget(key="enemy", target=MacroNameTarget(name="boss"))

    state, _clipped = build_context_state(reading, scope, (target,))

    assert state["enemy"]["path"] == BOSS


def test_a_target_that_is_not_found_raises_what_the_readers_raise() -> None:
    target = MacroContextTarget(key="gone", target=MacroSelectorTarget(selector="Root[0]/Gone[9]"))

    with pytest.raises(MacroFailure) as raised:
        build_context_state(battle(), MacroScope(), (target,))

    assert raised.value.code == SELECTOR_NOT_FOUND


def test_find_all_renders_every_match_and_zero_matches_is_an_empty_list() -> None:
    hand = MacroContextFindAll(
        key="hand", find=MacroFindValue(name=MacroStringLiteral(value="Card(Clone)"))
    )
    nothing = MacroContextFindAll(
        key="nothing", find=MacroFindValue(name=MacroStringLiteral(value="Nope"))
    )

    state, _clipped = build_context_state(battle(), MacroScope(), (hand, nothing))

    assert state["hand"] == [
        {"path": "Root[0]/Hand[2]/Card(Clone)[1]", "text": "Fire", "members": {}},
        {"path": "Root[0]/Hand[2]/Card(Clone)[2]", "text": "Shoot", "members": {}},
    ]
    assert state["nothing"] == []


def test_a_name_gives_the_value_bound_in_scope_and_a_literal_gives_itself() -> None:
    scope = MacroScope({"hp": 12.5})
    hp = MacroContextName(key="hp", name="hp", declared_type=MacroType.float_)
    goal = MacroContextLiteral(
        key="goal", literal=MacroLiteral(shape=MacroShape.string, value="beat the boss")
    )

    state, _clipped = build_context_state(battle(), scope, (hp, goal))

    assert state == {"scene": "TurnBattleScene", "hp": 12.5, "goal": "beat the boss"}


def test_no_argument_gives_every_live_object_sorted_by_key() -> None:
    state, clipped = build_context_state(battle(), MacroScope(), ())

    assert clipped == 0
    assert list(state) == ["scene", "objects"]
    assert [one["path"] for one in state["objects"]] == [
        "Root[0]/Boss_Undead(Clone)[4]",
        "Root[0]/Hand[2]/Card(Clone)[1]",
        "Root[0]/Hand[2]/Card(Clone)[2]",
    ]


def test_no_pulse_yet_gives_only_the_scene_and_no_objects() -> None:
    state, clipped = build_context_state(MacroMemories(scene=SceneMemory()), MacroScope(), ())

    assert state == {"scene": "", "objects": []}
    assert clipped == 0


def test_a_long_value_is_clipped_with_an_ellipsis() -> None:
    long_text = "a" * 200
    reading = memories(
        thing("Root[0]/Log[1]", text=long_text, members=[member("Game.Log", "Lines", ["x"] * 50)])
    )
    target = MacroContextTarget(key="log", target=MacroSelectorTarget(selector="Root[0]/Log[1]"))
    goal = MacroContextLiteral(
        key="goal", literal=MacroLiteral(shape=MacroShape.string, value="b" * 200)
    )

    state, _clipped = build_context_state(reading, MacroScope(), (target, goal))

    for clipped_value in (state["log"]["text"], state["log"]["members"]["Log.Lines"], state["goal"]):
        assert len(clipped_value) == MAX_CONTEXT_VALUE_CHARS
        assert clipped_value.endswith("…")
    # 문자열은 JSON 으로 적은 것을 자른다 — 앞 따옴표가 남는다.
    assert state["goal"].startswith('"bbb')


def test_a_state_over_the_limit_drops_objects_from_the_end_and_counts_them() -> None:
    crowd = memories(
        *(thing(f"Root[0]/Crowd[1]/Member[{index:03d}]", text="x" * 70) for index in range(200))
    )

    state, clipped = build_context_state(crowd, MacroScope(), ())

    assert clipped > 0
    assert len(state["objects"]) + clipped == 200
    assert len(json.dumps(state, ensure_ascii=False)) <= MAX_DECIDE_STATE_CHARS
    # 뒤에서부터 뺐으므로 남은 것이 앞쪽이다.
    assert state["objects"][0]["path"] == "Root[0]/Crowd[1]/Member[000]"
    assert state["scene"] == "TurnBattleScene"


def test_the_last_list_is_emptied_before_the_one_before_it() -> None:
    crowd = memories(
        *(thing(f"Root[0]/A[1]/Item[{index:03d}]", text="x" * 70) for index in range(60)),
        *(thing(f"Root[0]/B[1]/Item[{index:03d}]", text="y" * 70) for index in range(60)),
    )
    first = MacroContextFindAll(
        key="first", find=MacroFindValue(under=MacroStringLiteral(value="Root[0]/A[1]"))
    )
    second = MacroContextFindAll(
        key="second", find=MacroFindValue(under=MacroStringLiteral(value="Root[0]/B[1]"))
    )

    state, clipped = build_context_state(crowd, MacroScope(), (first, second))

    assert clipped == 120 - len(state["first"]) - len(state["second"])
    assert state["second"] == []
    assert 0 < len(state["first"]) < 60


# --- `under(...)` --------------------------------------------------------------


def hand_and_hint() -> MacroMemories:
    return memories(
        thing("Root[0]/Hand[2]", text="Hand"),
        thing("Root[0]/Hand[2]/Card(Clone)[1]", text="Fire"),
        thing("Root[0]/Hand[2]/Card(Clone)[1]/Glow[0]"),
        thing("Root[0]/HandHint[3]", text="Drag a card"),
        thing(BOSS, text="Undead"),
    )


def test_under_a_path_lists_the_object_and_everything_below_it_in_path_order() -> None:
    argument = MacroContextUnder(key="hand", root=MacroSelectorTarget(selector="Root[0]/Hand[2]"))

    state, clipped = build_context_state(hand_and_hint(), MacroScope(), (argument,))

    assert clipped == 0
    # `HandHint` 는 경로가 `Root[0]/Hand` 로 시작하지만 `Hand[2]` 아래가 아니다.
    assert [one["path"] for one in state["hand"]] == [
        "Root[0]/Hand[2]",
        "Root[0]/Hand[2]/Card(Clone)[1]",
        "Root[0]/Hand[2]/Card(Clone)[1]/Glow[0]",
    ]


def test_under_an_object_name_uses_that_object_s_path() -> None:
    held = hand_and_hint()
    scope = MacroScope()
    scope.bind(
        "hand",
        resolve_find(held, scope, MacroFindValue(label=MacroStringLiteral(value="Hand"))),
    )
    argument = MacroContextUnder(key="cards", root=MacroNameTarget(name="hand"))

    state, _ = build_context_state(held, scope, (argument,))

    assert len(state["cards"]) == 3


def test_under_a_path_with_nothing_below_is_an_empty_list() -> None:
    argument = MacroContextUnder(key="shop", root=MacroSelectorTarget(selector="Root[0]/Shop[9]"))

    state, _ = build_context_state(hand_and_hint(), MacroScope(), (argument,))

    assert state["shop"] == []


def test_under_is_clipped_from_the_end_like_find_all() -> None:
    many = [
        thing(f"Root[0]/Hand[2]/Card(Clone)[{index}]", text="x" * 70) for index in range(200)
    ]
    argument = MacroContextUnder(key="hand", root=MacroSelectorTarget(selector="Root[0]/Hand[2]"))

    state, clipped = build_context_state(memories(*many), MacroScope(), (argument,))

    assert clipped > 0
    assert len(state["hand"]) == 200 - clipped
    assert len(json.dumps(state, ensure_ascii=False)) <= MAX_DECIDE_STATE_CHARS
