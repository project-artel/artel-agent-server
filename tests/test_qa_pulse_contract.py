"""SDK 가 보낸 PULSE 를 agent 가 그대로 보관하고 같은 이름으로 읽는가(ARTEL-1289).

2026-10-28 점검이 재현한 열한 가지를 올바른 계약으로 고정한다. 입력은 SDK 가 실제로 내는 모양
(`LiveState`·`Lookup.Peek`·adapter 조각·attachment)을 작은 fixture 로 적은 것이고, 읽는 쪽은 실제
`PulseMemory`·reader·Jev context·macro runner 다.
"""

import asyncio

import pytest

from app.agents.planner_executer.macro_host import ChannelMacroHost
from app.agents.qa.macro.binding import (
    FoundObject,
    MacroMemories,
    MacroScope,
    read,
    sdk_member_name,
    unknown_member_names,
)
from app.agents.qa.macro.context import render_object
from app.agents.qa.macro.errors import ACTION_REJECTED, MacroFailure
from app.agents.qa.macro.model import MacroReaderCall, MacroSelectorTarget, MacroStringLiteral
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.runner import ReadMemberAnswer, _apply_read_member, run_macro
from app.qa.jev import JevClient
from app.qa.pulse import AmbiguousValue, PulseMemory, PulseReading, UnreadValue
from app.qa.scene import SceneMemory
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import FakeJev


def memory(active=(), statics=(), **extra) -> MacroMemories:
    scene = SceneMemory()
    scene.pulse.apply(
        PulseReading.model_validate(
            dict(scene="Battle", whole=True, reading=1, active=list(active), statics=list(statics), **extra)
        )
    )
    return MacroMemories(scene=scene)


def obj(members=(), path="Enemy", selector="Enemy[0]", **extra) -> dict:
    return dict(path=path, selector=selector, members=list(members), **extra)


def call(name: str, target: str | None = "Enemy[0]", reader: str = "member") -> MacroReaderCall:
    return MacroReaderCall(
        reader=reader,
        key=MacroStringLiteral(value=name),
        target=MacroSelectorTarget(selector=target) if target else None,
    )


def value(memories: MacroMemories, name: str, target: str | None = "Enemy[0]", reader: str = "member"):
    return read(memories, MacroScope(), call(name, target, reader))


def refusal(memories: MacroMemories, name: str, target: str | None = "Enemy[0]", reader: str = "member") -> MacroFailure:
    with pytest.raises(MacroFailure) as caught:
        value(memories, name, target, reader)
    assert caught.value.code == ACTION_REJECTED
    return caught.value


# --- 1. 컬렉션은 개수가 값이다 -----------------------------------------------------


def test_a_collection_member_reads_as_its_count_and_the_view_shows_it() -> None:
    m = memory([obj([{"on": "Cards.CardManager", "member": "myCards", "count": 44}])])

    assert value(m, "CardManager.myCards") == 44
    assert "CardManager.myCards = 44" in m.pulse.render(advance=False)


def test_a_read_member_answer_with_only_a_count_is_the_collections_size_not_a_holder_count() -> None:
    pulse = PulseMemory()
    _apply_read_member(pulse, "Cards.CardManager::myCards", ReadMemberAnswer(values=[{"count": 44}]))

    assert pulse.read_members["Cards.CardManager::myCards"].count == 44
    assert pulse.unreadable_members == {}
    m = memory([obj()])
    _apply_read_member(m.pulse, "Cards.CardManager::myCards", ReadMemberAnswer(values=[{"count": 44}]))
    assert value(m, "CardManager.myCards", None, "static") == 44


# --- 2. adapter 조각 --------------------------------------------------------------------


def test_adapter_fragments_are_kept_drawn_and_readable() -> None:
    m = memory(
        [
            obj(
                ui={"interactable": False, "fill": 1},
                engine={"audio": {"playing": True, "clip": "Pixel 1"}},
                uitk=[{"name": "score", "type": "Label", "text": "77", "enabled": True, "visible": False}],
            )
        ]
    )
    held = m.pulse.held["Battle/Enemy[0]"]

    assert held.ui == {"interactable": False, "fill": 1} and held.engine["audio"]["clip"] == "Pixel 1"
    rendered = m.pulse.render(advance=False)
    assert "  ui: fill=1 interactable=False" in rendered
    assert "  engine: audio.clip='Pixel 1' audio.playing=True" in rendered
    assert "  uitk: score(Label) '77' not visible" in rendered
    assert value(m, "ui.interactable") is False and value(m, "engine.audio.playing") is True
    assert value(m, "uitk.score.text") == "77"
    jev = render_object(m, FoundObject("Battle/Enemy[0]", held))
    assert jev["ui"] == {"interactable": False, "fill": 1} and jev["engine"]["audio"]["playing"] is True


def test_a_fragment_that_did_not_arrive_in_a_delta_is_kept_and_one_that_did_replaces_the_old() -> None:
    m = memory([obj(ui={"interactable": False}, engine={"audio": {"playing": True}})])
    m.pulse.apply(
        PulseReading.model_validate(
            dict(scene="Battle", reading=2, active=[obj(ui={"interactable": True})], changed=["Battle/Enemy[0]|ui"])
        )
    )
    held = m.pulse.held["Battle/Enemy[0]"]

    assert held.ui == {"interactable": True} and held.engine == {"audio": {"playing": True}}
    assert "  ui: interactable=True" in m.pulse.render(since=1, advance=False)


# --- 3. 못 읽은 멤버 -----------------------------------------------------------------


def test_an_unread_member_is_refused_with_the_games_reason_and_drawn_as_unread() -> None:
    m = memory([obj([{"on": "Enemy", "member": "hp", "unread": "TargetInvocationException"}])])

    failure = refusal(m, "Enemy.hp")
    assert "could not be read by the game: TargetInvocationException" in failure.reason
    assert "Enemy.hp = (unread: TargetInvocationException)" in m.pulse.render(advance=False)
    assert isinstance(m.pulse.held["Battle/Enemy[0]"].members["Enemy::hp#0"].observed, UnreadValue)


def test_a_null_the_game_read_is_still_none() -> None:
    m = memory([obj([{"on": "Enemy", "member": "target", "value": None}])])

    assert value(m, "Enemy.target") is None


# --- 4. holder 가 여럿인 static --------------------------------------------------------


def test_statics_held_by_several_objects_keep_one_entry_each_and_the_bare_name_is_ambiguous() -> None:
    m = memory(
        statics=[
            {"declaring": "Game.State", "member": "hp", "instance": 0, "value": 5},
            {"declaring": "Game.State", "member": "hp", "instance": 1, "value": 9},
        ]
    )

    assert sorted(m.pulse.statics) == ["Game.State::hp@0", "Game.State::hp@1"]
    assert value(m, "State.hp@1", None, "static") == 9
    failure = refusal(m, "State.hp", None, "static")
    assert "names 2 values that differ" in failure.reason and "'Game.State::hp@0'" in failure.reason
    rendered = m.pulse.render(advance=False)
    assert "State.hp@0 = 5" in rendered and "State.hp@1 = 9" in rendered


def test_statics_held_by_several_objects_with_the_same_value_read_as_one() -> None:
    m = memory(
        statics=[
            {"declaring": "Game.State", "member": "hp", "instance": 0, "value": 5},
            {"declaring": "Game.State", "member": "hp", "instance": 1, "value": 5},
        ]
    )

    assert value(m, "State.hp", None, "static") == 5


# --- 5. 같은 component 둘: Jev 와 macro 가 같은 값을 읽는다 ------------------------------


def test_two_components_of_one_type_are_ambiguous_by_the_short_name_and_exact_by_among() -> None:
    m = memory([obj([{"on": "Enemy", "member": "hp", "value": 5}, {"on": "Enemy", "member": "hp", "among": 1, "value": 9}])])

    assert value(m, "Enemy.hp#1") == 9 and value(m, "Enemy::hp#0") == 5
    for name in ("Enemy.hp", "Enemy::hp", "hp"):
        refusal(m, name)
    jev = render_object(m, FoundObject("Battle/Enemy[0]", m.pulse.held["Battle/Enemy[0]"]))
    assert jev["members"] == {"Enemy.hp#0": 5, "Enemy.hp#1": 9}


# --- 6. 한 이름 해석기 ---------------------------------------------------------------


def test_every_spelling_of_one_member_reads_the_same_value() -> None:
    m = memory([obj([{"on": "Cards.CardManager", "member": "hp", "value": 5}])])

    for name in ("Cards.CardManager::hp", "Cards.CardManager.hp", "CardManager.hp", "hp"):
        assert value(m, name) == 5, name


def test_a_fetched_value_is_found_under_the_full_dotted_name_the_macro_wrote() -> None:
    m = memory([obj()])
    _apply_read_member(m.pulse, "Battle.Turns.TurnBattleSystem::turn", ReadMemberAnswer(values=[{"value": 7}]))

    assert value(m, "Battle.Turns.TurnBattleSystem.turn", None, "static") == 7
    assert value(m, "TurnBattleSystem.turn", None, "static") == 7


# --- 7. 점 경로 멤버 -----------------------------------------------------------------


def test_a_dotted_member_path_is_cut_at_a_type_the_pulse_knows() -> None:
    m = memory([obj([{"on": "Cards.CardManager", "member": "hp", "value": 5}])])

    assert sdk_member_name("CardManager.myCards.Count", m.pulse) == "Cards.CardManager::myCards.Count"
    assert sdk_member_name("Cards.CardManager.myCards.Count", m.pulse) == "Cards.CardManager::myCards.Count"
    assert sdk_member_name("Cards.CardManager::myCards.Count", m.pulse) == "Cards.CardManager::myCards.Count"
    # type 을 모르면 종전처럼 마지막 마디가 member 다.
    assert sdk_member_name("Game.Score", m.pulse) == "Game::Score"


# --- 8·9. target 의 값만 -----------------------------------------------------------


def test_a_name_another_object_holds_is_still_unknown_on_the_target() -> None:
    m = memory([obj(), obj([{"on": "Enemy", "member": "hp", "value": 99}], path="Other", selector="Other[1]")])

    assert unknown_member_names(m, MacroScope(), [call("Enemy.hp")]) == ["Enemy.hp"]
    assert unknown_member_names(m, MacroScope(), [call("Enemy.hp", "Other[1]")]) == []


def test_a_read_member_answer_is_kept_per_holder_and_an_unread_holder_is_refused() -> None:
    m = memory([obj(), obj(path="Other", selector="Other[1]")])
    _apply_read_member(
        m.pulse,
        "Enemy::hp",
        ReadMemberAnswer(values=[{"path": "Other", "value": 7}, {"path": "Enemy", "unread": "no-holder"}]),
    )

    assert value(m, "Enemy.hp", "Other[1]") == 7
    failure = refusal(m, "Enemy.hp")
    assert "no-holder" in failure.reason
    # holder 를 가리지 않는 값은 없다 — 하나는 읽고 하나는 못 읽었다.
    assert "Enemy::hp" not in m.pulse.read_members


# --- 10. 잘린 reading -------------------------------------------------------------------


def test_a_truncated_reading_is_said_and_absence_is_not_decided_on_it() -> None:
    m = memory([obj()], gaps=["holder-limit:8"])

    assert "coverage gaps: holder-limit:8" in m.pulse.render(advance=False)
    assert value(m, "", "Missing", "exists") is False
    with pytest.raises(MacroFailure) as caught:
        read(m, MacroScope(), MacroReaderCall(reader="absent", target=MacroSelectorTarget(selector="Missing")))
    assert "truncated (holder-limit:8)" in caught.value.reason
    # 보이는 객체의 부재는 그대로 거짓이다.
    assert read(m, MacroScope(), MacroReaderCall(reader="absent", target=MacroSelectorTarget(selector="Enemy[0]"))) is False


def test_without_gaps_absence_is_decided_as_before() -> None:
    m = memory([obj()])

    assert read(m, MacroScope(), MacroReaderCall(reader="absent", target=MacroSelectorTarget(selector="Missing"))) is True


# --- 11. 입력 뒤에는 다시 묻는다 ----------------------------------------------------------


def test_a_fetched_value_is_asked_again_after_an_input() -> None:
    fake = FakeGame("Battle", {"Root/Enemy": {"text": "Enemy", "members": {}}})
    fake.answers["read_member"] = lambda params: {"values": [{"value": 0}]}
    fake.reactions[("press", "Root/Enemy")] = lambda game: game.answers.update(
        read_member=lambda params: {"values": [{"value": 1}]}
    )
    source = (
        "def check() -> None:\n"
        '    require(static("Game.Score") == 0, "before")\n'
        '    enemy: object = find(name="Enemy")\n'
        "    click(enemy)\n"
        '    require(static("Game.Score") == 1, "after")\n'
    )

    async def go():
        fake.push_reading()
        host = ChannelMacroHost(fake.channel, JevClient(post=FakeJev()), 1)
        return await run_macro(host, macro_definition_from_source("check", source), {}, 1)

    result = asyncio.run(go())

    assert result.failure is None
    assert [params for method, params in fake.actions if method == "read_member"] == [["Game::Score"], ["Game::Score"]]


# --- 소리와 예외 --------------------------------------------------------------------------


def test_audio_and_errors_reach_the_view_as_state_and_events() -> None:
    m = memory(
        [obj()],
        audio={"playing": 1, "started": [{"clip": "Pixel 1", "frame": 2429}]},
        errors=[{"type": "Exception", "message": "boom", "stackHead": "A ()", "firstFrame": 20, "lastFrame": 20, "count": 3}],
    )
    rendered = m.pulse.render(advance=False)

    assert "audio playing: 1" in rendered
    assert "  audio.started Pixel 1" in rendered
    assert "  error Exception: boom (x3)" in rendered


def test_a_reading_without_the_new_fields_renders_as_before() -> None:
    plain = memory([obj([{"on": "Enemy", "member": "hp", "value": 5}])]).pulse.render(advance=False)

    assert "coverage gaps" not in plain and "audio playing" not in plain and "ui:" not in plain
    assert "Enemy.hp = 5" in plain


def test_wait_until_asks_a_fetched_value_again_on_every_new_reading() -> None:
    """`pulse` 가 감시하지 않는 값은 reading 이 갱신하지 않는다. `wait_until` 이 statement 앞에 받은 값만
    보면 끝까지 거짓이다(런 154 의 `end_turn`). 새 reading 마다 다시 묻는다."""
    fake = FakeGame("Battle", {"Root/Enemy": {"text": "Enemy", "members": {}}})
    score = {"value": 0}
    fake.answers["read_member"] = lambda params: {"values": [{"value": score["value"]}]}
    source = (
        "def check() -> None:\n"
        '    wait_until(static("Game.Score") == 1, 2, "Score never reached 1.")\n'
    )

    async def go():
        fake.push_reading()
        loop = asyncio.get_running_loop()

        def bump() -> None:
            score["value"] = 1
            fake.push_reading()

        loop.call_later(0.05, bump)
        host = ChannelMacroHost(fake.channel, JevClient(post=FakeJev()), 1)
        return await run_macro(host, macro_definition_from_source("check", source), {}, 1)

    result = asyncio.run(go())

    assert result.failure is None
    assert [params for method, params in fake.actions if method == "read_member"] == [["Game::Score"], ["Game::Score"]]
