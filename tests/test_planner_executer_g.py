"""`g`, what a step module's Python reads: values and waits over the real channel. It sends no input:
func 의 `run` macro 가 보낸다(`test_planner_executer_macro.py`)."""

import asyncio

import pytest

from app.agents.planner_executer.g import (
    ExpectationFailed,
    GameHandle,
    WaitTimedOut,
    g_functions,
)
from tests.planner_executer_fakes import FakeGame


def _game(**objects) -> FakeGame:
    return FakeGame("Menu", objects or {"Root/Start": {"text": "Start", "members": {"Button.interactable": True}}})


def _play(fake: FakeGame, body) -> None:
    """Run `body(g)` with the handle watching readings, as the executer runs it."""

    async def run() -> None:
        g = GameHandle(fake.channel)
        watcher = asyncio.create_task(g.watch())
        fake.push_reading()
        await asyncio.sleep(0)
        try:
            await body(g)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())


def test_reads_take_a_member_by_its_name_alone_or_with_its_component() -> None:
    fake = FakeGame(
        "Battle",
        {"Root/Hero": {"text": "Hero", "members": {"Unit.hp": 30, "Unit.mana": 2}}},
    )

    async def body(g: GameHandle) -> None:
        assert g.scene == "Battle"
        assert g.value("hero", "hp") == 30
        assert g.value("hero", "Unit.mana") == 2
        assert g.text("hero") == "Hero"
        assert g.find("nothing") is None

    _play(fake, body)


def test_wait_until_returns_on_the_reading_that_shows_the_condition() -> None:
    fake = _game()

    async def body(g: GameHandle) -> None:
        asyncio.get_running_loop().call_later(0.05, lambda: (setattr(fake, "scene", "Map"), fake.push_reading()))
        await g.wait_until(lambda: g.scene == "Map", timeout=3)
        assert g.scenes_visited() == ["Map"] or g.scenes_visited()[-1] == "Map"

    _play(fake, body)


def test_a_condition_that_never_holds_times_out_and_a_failed_expect_fails_the_step() -> None:
    fake = _game()

    async def body(g: GameHandle) -> None:
        with pytest.raises(WaitTimedOut):
            await g.wait_until(lambda: g.scene == "Map", timeout=0.2)
        with pytest.raises(ExpectationFailed, match="the map opened"):
            await g.expect(lambda: g.scene == "Map", "the map opened", timeout=0.2)

    _play(fake, body)


def test_a_condition_that_raises_has_not_held_yet() -> None:
    fake = _game()

    async def body(g: GameHandle) -> None:
        with pytest.raises(WaitTimedOut):
            await g.wait_until(lambda: g.value("start", "missing") + 1 > 0, timeout=0.2)
        assert any("condition raised TypeError" in line for line in g.trace)

    _play(fake, body)


def test_check_can_ask_about_a_state_play_passed_through() -> None:
    fake = FakeGame("Battle", {"Root/Reward": {"text": "You won 3 coins"}})

    async def body(g: GameHandle) -> None:
        g.begin_step(1)
        fake.objects = {"Root/Map": {"text": "World map"}}
        fake.scene = "Map"
        fake.push_reading()
        await g.next_reading(1.0)
        await asyncio.sleep(0)
        assert g.seen_text("3 coins")
        assert g.seen_object("Reward")
        assert g.scenes_visited() == ["Battle", "Map"]

    _play(fake, body)


def test_a_value_from_the_start_of_the_step_is_kept_for_a_change_claim() -> None:
    fake = FakeGame("Map", {"Root/Stage": {"text": "Stage 0", "members": {"Stage.position": 0}}})

    async def body(g: GameHandle) -> None:
        g.begin_step(2)
        fake.objects["Root/Stage"] = {"text": "Stage 1", "members": {"Stage.position": 1}}
        fake.push_reading()
        await g.next_reading(1.0)
        assert g.value("stage", "position") == 1
        assert g.at_step_start(2, "Stage.position", match="stage") == 0
        assert g.at_step_start(2, "text", match="stage") == "Stage 0"

    _play(fake, body)


def test_the_state_view_spells_every_member_the_way_a_function_reads_it() -> None:
    fake = FakeGame("Battle", {"Root/Hero": {"members": {f"Unit.m{i}": i for i in range(30)}}})

    async def body(g: GameHandle) -> None:
        view = g.state_view()
        assert "scene 'Battle'" in view
        assert all(f"Unit.m{i} = {i}" in view for i in range(30))

    _play(fake, body)


def test_an_operator_message_does_not_hold_a_wait_open() -> None:
    fake = _game()

    async def body(g: GameHandle) -> None:
        fake.channel.on_chat({"type": "CHAT", "payload": {"message": "keep going"}})
        assert await g.next_reading(0.3) is False
        assert any("operator said: keep going" in line for line in g.trace)

    _play(fake, body)


def test_the_function_list_is_what_the_fingerprint_hashes() -> None:
    functions = g_functions()

    assert {"wait_until", "expect", "value", "scene"} <= set(functions)
    # 입력은 func 의 macro 가 보낸다. `g` 에는 입력 함수가 없다.
    assert not {"click", "press_key", "reset_game"} & set(functions)
    assert not any(name.startswith("_") for name in functions)
