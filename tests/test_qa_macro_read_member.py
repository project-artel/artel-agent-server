"""`static()`·`member()` 가 `pulse` 에 없는 이름을 만나면 runner 가 게임에 `read_member` 로 한 번 묻는다
(ARTEL-1285). 받은 값으로 조건이 평가되고, 못 읽은 이름은 종전의 `ACTION_REJECTED` 에 SDK 의 이유가 붙는다.
"""

import asyncio

import pytest

from app.agents.planner_executer.macro_host import ChannelMacroHost
from app.agents.qa.macro.binding import (
    MacroMemories,
    MacroScope,
    read,
    sdk_member_name,
    unknown_member_names,
)
from app.agents.qa.macro.errors import ACTION_REJECTED, SCENE_MISMATCH
from app.agents.qa.macro.model import (
    MacroReaderCall,
    MacroSelectorTarget,
    MacroStringLiteral,
)
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.runner import ReadMemberAnswer, run_macro
from app.qa.envelope import ActionResultItem
from app.qa.jev import JevClient
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import FakeJev

SCORE_IS_SEVEN = (
    "def check() -> None:\n"
    '    require(static("Game.Score") == 7, "Score should read 7.")\n'
    '    require(static("Game.Score") < 10, "Score should stay under 10.")\n'
)


def _game() -> FakeGame:
    return FakeGame(
        "Battle", {"Root/Enemy": {"text": "Enemy", "members": {"Enemy.Hp": 42}}}
    )


def _answer(**fields):
    base = {
        "resolved": [],
        "unresolvable": [],
        "walk": {"visited": 3, "ms": 0.1},
        "values": [],
    }
    base.update(fields)
    return lambda params: base


def _run(fake: FakeGame, source: str, entry: str = "check"):
    definition = macro_definition_from_source(entry, source)

    async def go():
        fake.push_reading()
        host = ChannelMacroHost(fake.channel, JevClient(post=FakeJev()), step=1)
        return await run_macro(host, definition, {}, step=1)

    return asyncio.run(go())


def _sent(fake: FakeGame) -> list[tuple[str, list]]:
    return [
        (method, params) for method, params in fake.actions if method == "read_member"
    ]


def test_a_static_the_pulse_does_not_carry_is_read_from_the_game_once_and_then_evaluated() -> (
    None
):
    fake = _game()
    fake.answers["read_member"] = _answer(
        resolved=[
            {
                "name": "Game::Score",
                "kind": "static",
                "holders": 1,
                "declaring": "Game",
                "member": "Score",
            }
        ],
        values=[{"value": 7}],
    )

    result = _run(fake, SCORE_IS_SEVEN)

    assert result.failure is None
    # 두 `require` 가 같은 이름을 읽지만 게임에는 한 번만 물었다.
    assert _sent(fake) == [("read_member", ["Game::Score"])]
    assert fake.channel.scene.pulse.read_members["Game::Score"].value == 7


def test_a_name_the_game_cannot_resolve_is_rejected_with_the_games_reason() -> None:
    fake = _game()
    fake.answers["read_member"] = _answer(
        unresolvable=[{"name": "Game::Score", "reason": "no such type"}]
    )

    result = _run(fake, SCORE_IS_SEVEN)

    assert result.failure.code == ACTION_REJECTED
    assert (
        "static('Game.Score') is not a static this run has been sent"
        in result.failure.reason
    )
    assert (
        "`read_member`" in result.failure.reason
        and "no such type" in result.failure.reason
    )
    assert _sent(fake) == [("read_member", ["Game::Score"])]


def test_a_name_already_in_the_pulse_sends_nothing() -> None:
    fake = _game()
    fake.answers["read_member"] = _answer(values=[{"value": 99}])
    original = fake.push_reading

    def with_static() -> None:
        original()
        fake.channel.on_pulse(
            {
                "type": "PULSE",
                "payload": {
                    "schema": 2,
                    "reading": fake.reading + 1,
                    "frame": fake.frame + 1,
                    "scene": fake.scene,
                    "whole": False,
                    "active": [],
                    "changed": [],
                    "statics": [{"declaring": "Game", "member": "Score", "value": 7}],
                },
            }
        )

    fake.push_reading = with_static

    result = _run(fake, SCORE_IS_SEVEN)

    assert result.failure is None
    assert _sent(fake) == []


def test_a_member_missing_on_the_object_is_read_from_the_game_when_one_instance_holds_it() -> (
    None
):
    fake = _game()
    fake.answers["read_member"] = _answer(
        resolved=[{"name": "Enemy::Shield", "kind": "instance", "holders": 1}],
        values=[{"value": 3}],
    )
    source = (
        "def check() -> None:\n"
        '    enemy: object = find(name="Enemy")\n'
        '    require(member(enemy, "Enemy.Shield") == 3, "The enemy should carry a shield of 3.")\n'
    )

    result = _run(fake, source)

    assert result.failure is None
    assert _sent(fake) == [("read_member", ["Enemy::Shield"])]


def test_instances_with_different_values_are_not_learned() -> None:
    fake = _game()
    fake.answers["read_member"] = _answer(values=[{"value": 3}, {"value": 5}])
    source = (
        "def check() -> None:\n"
        '    enemy: object = find(name="Enemy")\n'
        '    require(member(enemy, "Enemy.Shield") == 3, "The enemy should carry a shield of 3.")\n'
    )

    result = _run(fake, source)

    assert result.failure.code == ACTION_REJECTED
    assert "2 instances hold this member with different values" in result.failure.reason


def test_an_sdk_without_read_member_leaves_the_rejection_with_its_refusal() -> None:
    fake = _game()
    fake.refuse.add(("read_member", "Game::Score"))

    result = _run(fake, SCORE_IS_SEVEN)

    assert result.failure.code == ACTION_REJECTED
    assert "Nothing at selector: Game::Score" in result.failure.reason


def test_a_fetched_value_that_fails_the_condition_stops_as_before() -> None:
    fake = _game()
    fake.answers["read_member"] = _answer(values=[{"value": 8}])

    result = _run(fake, SCORE_IS_SEVEN)

    assert result.failure.code == SCENE_MISMATCH
    assert result.failure.payload["observed"]["static(Game.Score)"] == "8"


# --- binding -------------------------------------------------------------------


@pytest.mark.parametrize(
    "dotted, expected",
    [
        ("Game.Score", "Game::Score"),
        ("Battle.Turns.TurnBattleSystem.turn", "Battle.Turns.TurnBattleSystem::turn"),
        ("Game::Score", "Game::Score"),
        ("Score", None),
        (".Score", None),
    ],
)
def test_the_macro_name_becomes_the_sdk_name(dotted: str, expected: str | None) -> None:
    assert sdk_member_name(dotted) == expected


def _memories() -> MacroMemories:
    scene = SceneMemory()
    scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": "Battle",
                "whole": True,
                "statics": [
                    {"declaring": "Game.Flags", "member": "Locked", "value": True}
                ],
                "active": [
                    {
                        "selector": "Root[0]/Enemy[1]",
                        "path": "Root/Enemy",
                        "id": 4,
                        "members": [{"on": "Enemy", "member": "Hp", "value": 42}],
                    }
                ],
            }
        )
    )
    return MacroMemories(scene=scene)


def _call(reader: str, key: str, target: str | None = None) -> MacroReaderCall:
    return MacroReaderCall(
        reader=reader,
        key=MacroStringLiteral(value=key),
        target=MacroSelectorTarget(selector=target) if target else None,
    )


def test_unknown_names_skip_what_the_pulse_watches_and_what_read_member_already_learned() -> (
    None
):
    memories = _memories()
    memories.pulse.learn_member("Game", "Score", 7)
    calls = [
        _call("static", "Flags.Locked"),
        _call("static", "Game.Score"),
        _call("member", "Enemy.Hp", "Root[0]/Enemy[1]"),
        _call("member", "Enemy.Shield", "Root[0]/Enemy[1]"),
        _call("static", "Game.Score"),
        _call("scene", "ignored"),
    ]

    # 전에 `read_member` 로 배운 값은 그때의 값이라 "있다" 로 세지 않는다 — 다음 run 이 다시 묻는다.
    assert unknown_member_names(memories, MacroScope(), calls) == [
        "Game.Score",
        "Enemy.Shield",
    ]


def test_readers_fall_back_to_what_read_member_learned_and_name_its_refusal() -> None:
    memories = _memories()
    memories.pulse.learn_member("Game", "Score", 7)
    memories.pulse.refuse_member(
        "Game", "Lives", "field is private and the SDK does not read privates"
    )

    assert read(memories, MacroScope(), _call("static", "Game.Score")) == 7
    assert (
        read(memories, MacroScope(), _call("member", "Game.Score", "Root[0]/Enemy[1]"))
        == 7
    )
    with pytest.raises(Exception) as failed:
        read(memories, MacroScope(), _call("static", "Game.Lives"))
    assert failed.value.code == ACTION_REJECTED
    assert "does not read privates" in failed.value.reason
    # `pulse` 가 감시하는 값이 먼저다.
    memories.pulse.learn_member("Game.Flags", "Locked", False)
    assert read(memories, MacroScope(), _call("static", "Flags.Locked")) is True


def test_the_answer_model_turns_a_refused_action_into_an_unresolvable_entry() -> None:
    refused = ReadMemberAnswer.from_result(
        ActionResultItem(id=1, success=False, error="unknown method read_member"),
        "Game::Score",
    )
    assert refused.unresolvable[0].reason == "unknown method read_member"
    assert ReadMemberAnswer.from_result(None, "Game::Score") is None
    answered = ReadMemberAnswer.from_result(
        ActionResultItem(
            id=1, success=True, returnValue={"values": [{"value": None}, {"count": 2}]}
        ),
        "Game::Score",
    )
    assert [one.read for one in answered.values] == [True, False]


def test_while_조건은_입력_후_read_member를_다시_조회한다() -> None:
    fake = _game()
    score = 0

    def changed(game):
        nonlocal score
        score = 1
        game.objects["Root/Enemy"]["text"] = "Changed"

    fake.reactions[("press", "Root/Enemy")] = changed
    fake.answers["read_member"] = lambda params: {"values": [{"value": score}]}
    result = _run(fake, '\n'.join([
        "def check() -> None:",
        '    while static("Game.Score") == 0:',
        '        click(selector("Root/Enemy"))',
    ]))

    assert result.failure is None
    assert _sent(fake) == [("read_member", ["Game::Score"])] * 2
    assert sum(method == "mouse_down" for method, _ in fake.actions) == 1


@pytest.mark.parametrize("after_input", [False, True])
def test_read_member_응답이_없으면_이전_조회값을_쓰지_않는다(after_input: bool) -> None:
    fake = _game()
    fake.reactions[("press", "Root/Enemy")] = lambda game: game.objects["Root/Enemy"].update(text="Changed")
    calls = []

    async def read_member(name):
        calls.append(name)
        return ReadMemberAnswer(values=[{"value": 0}]) if len(calls) == 1 else None

    async def run():
        fake.push_reading()
        host = ChannelMacroHost(fake.channel, JevClient(post=FakeJev()), step=1)
        host.read_member = read_member
        first = 'def check() -> None:\n    require(static("Game.Score") == 0, "initial")\n'
        if after_input:
            source = first + '    click(selector("Root/Enemy"))\n    require(static("Game.Score") == 0, "after input")\n'
        else:
            previous = await run_macro(host, macro_definition_from_source("check", first), {}, step=1)
            assert previous.failure is None
            source = first
        return await run_macro(host, macro_definition_from_source("check", source), {}, step=1)

    result = asyncio.run(run())
    assert result.failure.code == ACTION_REJECTED
    assert calls == ["Game::Score", "Game::Score"]
    assert "Game::Score" not in fake.channel.scene.pulse.read_members


@pytest.mark.parametrize("function", [
    'def ready(key: string) -> bool:\n    return static(key) == 7\n',
    'def ready(key: string) -> bool:\n    local_key: string = "Game.Score"\n    return static(local_key) == 7\n',
    'def score(key: string) -> int:\n    return static(key)\ndef ready(key: string) -> bool:\n    return score(key) == 7\n',
    'def ready(key: string) -> bool:\n    local_key: string = static("Game.Key")\n    return static(local_key) == 7\n',
])
def test_def_내부_reader는_인자와_지역_scope에서_이름을_해석한다(function: str) -> None:
    fake = _game()
    fake.answers["read_member"] = lambda params: {"values": [{"value": "Game.Score" if params[0] == "Game::Key" else 7}]}
    source = function + '\ndef check() -> None:\n    require(ready("Game.Score"), "score should be seven")\n'
    result = _run(fake, source)
    assert result.failure is None
    assert ("read_member", ["Game::Score"]) in _sent(fake)


def test_def_reader는_선택된_branch만_준비한다() -> None:
    fake = _game()
    fake.answers["read_member"] = lambda params: {"values": [{"value": 7}]}
    source = '\n'.join([
        'def ready(key: string) -> bool:',
        '    if static("Game.Mode") == 7:',
        '        return static(key) == 7',
        '    return static("Game.Unchosen") == 7',
        'def check() -> None:',
        '    require(ready("Game.Score"), "should be ready")',
    ])
    assert _run(fake, source).failure is None
    assert _sent(fake) == [("read_member", ["Game::Mode"]), ("read_member", ["Game::Score"])]


def test_def_member_reader는_object_인자를_받은_scope에서_조회한다() -> None:
    fake = _game()
    fake.answers["read_member"] = _answer(values=[{"value": 7}])
    source = '\n'.join([
        'def ready(target: object, key: string) -> bool:',
        '    return member(target, key) == 7',
        'def check() -> None:',
        '    enemy: object = find(name="Enemy")',
        '    require(ready(enemy, "Enemy.Shield"), "should have shield")',
    ])
    assert _run(fake, source).failure is None
    assert _sent(fake) == [("read_member", ["Enemy::Shield"])]
