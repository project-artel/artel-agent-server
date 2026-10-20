"""The planner–executer engine on a FREE scenario (ARTEL-1149).

A FREE scenario is test cases in no order. The next case is the one checked in the scene on
screen; a case whose play never reached its goal and failed is put off, the game is reset after
the round, and the case is played again; what is still left is reported failed as UNREACHED.
Same fakes as `test_planner_executer_steps.py`.
"""

import asyncio

import pytest

from app.agents.planner_executer import runner as runner_module
from app.agents.planner_executer.free import FreeCase, pick_case
from app.agents.planner_executer.prompts import this_step
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _config, _judge, _menu, _verdicts


@pytest.fixture(autouse=True)
def _quick_reset(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "RESET_READING_SECONDS", 0.05)


def _case(case_id: int, scene: str, step: str, expected: str) -> QaStep:
    return QaStep(
        action="",
        case_id=case_id,
        case=QaCaseRef(id=case_id, scene=scene, test_step=step, expected=expected),
    )


def _free(*steps: QaStep) -> QaScenario:
    return QaScenario(title="Free", steps=list(steps), execution_mode="FREE")


MAP_CASE = _case(7, "Map", "Look at the map.", "Stage 0 is shown.")
MENU_CASE = _case(8, "Menu", "Press Start.", "The map opens.")


def _resettable_menu() -> FakeGame:
    """`_menu`, and `reset_game` takes the game back to the menu."""
    fake = _menu()

    def back_to_menu(game: FakeGame) -> None:
        game.scene = "Menu"
        game.objects = {"Root/Start": {"text": "Start"}}

    fake.reactions[("reset_game", None)] = back_to_menu
    return fake


def _run_free(fake: FakeGame, scenario: QaScenario, asked: Asked, judge, jev: FakeJev, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, scenario)

    return asyncio.run(go())


def _resets(fake: FakeGame) -> int:
    return sum(1 for method, _ in fake.actions if method == "reset_game")


# --- picking the next case -----------------------------------------------------------------------


def test_a_case_of_the_scene_on_screen_is_picked_first() -> None:
    pending = [FreeCase(1, "Map"), FreeCase(2, "Menu"), FreeCase(3, "menu")]

    assert pick_case(pending, "Menu").step == 2
    assert pick_case(pending, " MENU ").step == 2
    assert pick_case(pending[2:], "Menu").step == 3


def test_without_a_case_of_this_scene_the_first_written_case_is_picked() -> None:
    pending = [FreeCase(4, "Battle"), FreeCase(2, None)]

    assert pick_case(pending, "Menu").step == 4
    assert pick_case(pending, None).step == 4
    with pytest.raises(ValueError):
        pick_case([], "Menu")


# --- what the planner reads ----------------------------------------------------------------------


def test_a_free_step_shows_its_scene_what_to_do_and_what_to_verify() -> None:
    shown = this_step(_free(MAP_CASE, MENU_CASE), 2)

    assert '"scene": "Menu"' in shown
    assert '"do": "Press Start."' in shown
    assert '"verify": "The map opens."' in shown
    assert "(no action)" not in shown


def test_an_ordered_step_shows_no_scene() -> None:
    ordered = QaScenario(steps=[QaStep(action="Press Start.", case_id=8, case=MENU_CASE.case)])

    assert '"scene"' not in this_step(ordered, 1)


# --- playing a FREE scenario ---------------------------------------------------------------------


def test_the_case_of_the_scene_on_screen_is_played_first() -> None:
    fake = _resettable_menu()
    judge, seen = _judge({"passed": True, "reason": "The map opens."}, {"passed": True, "reason": "Stage 0."})

    state, failure = _run_free(fake, _free(MAP_CASE, MENU_CASE), Asked(MACROS, MACROS), judge, FakeJev())

    assert failure is None and state.finished
    # The game starts on the menu: the menu's case (step 2) goes first, then the map's.
    assert [(v["step"], v["case_id"], v["status"]) for v in _verdicts(fake)] == [
        (2, 8, "COMPLETED"),
        (1, 7, "COMPLETED"),
    ]
    assert all(v["is_verification"] for v in _verdicts(fake))
    assert '"do": "Press Start."' in seen[0][1]
    assert _resets(fake) == 0
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED" and closing["summary"]["cases"]["passed"] == 2


def test_a_case_never_reached_is_put_off_and_played_again_after_a_reset() -> None:
    fake = _resettable_menu()
    # First play only looks around and never reaches the map; the second presses Start.
    jev = FakeJev(picks=("look_around", "look_around", "look_around", "press_start"))
    judge, _ = _judge(
        {"passed": False, "reason": "The menu is still shown."},
        {"passed": True, "reason": "The map opens."},
    )

    _run_free(fake, _free(MENU_CASE), Asked(MACROS, MACROS), judge, jev, max_turns_per_step=3)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and verdict["case_id"] == 8
    assert _resets(fake) == 1


def test_a_case_still_unreached_after_the_limits_is_reported_unreached() -> None:
    fake = _resettable_menu()
    judge, _ = _judge(
        {"passed": False, "reason": "The menu is still shown."},
        {"passed": False, "reason": "Still the menu."},
    )

    _run_free(
        fake, _free(MENU_CASE), Asked(MACROS, MACROS), judge, FakeJev(picks=("look_around",)),
        max_turns_per_step=3,
    )

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith(
        "UNREACHED (도달 못함): play never reached the case's goal in 2 plays and 1 reset."
    )
    assert "Still the menu." in verdict["message"]
    assert _resets(fake) == 1


def test_without_resets_a_round_ends_the_scenario() -> None:
    fake = _resettable_menu()
    judge, _ = _judge({"passed": False, "reason": "The menu is still shown."})

    _run_free(
        fake, _free(MENU_CASE), Asked(MACROS), judge, FakeJev(picks=("look_around",)),
        max_turns_per_step=3, free_max_resets=0,
    )

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith("UNREACHED (도달 못함): play never reached the case's goal in 1 play and 0 resets.")
    assert _resets(fake) == 0


def test_a_case_that_reached_its_goal_and_failed_is_reported_without_a_retry() -> None:
    fake = _resettable_menu()
    judge, _ = _judge({"passed": False, "reason": "The map shows Stage 1, not Stage 0."})

    _run_free(fake, _free(MENU_CASE), Asked(MACROS), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"] == "The map shows Stage 1, not Stage 0."
    assert _resets(fake) == 0
