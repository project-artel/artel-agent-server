"""The planner–executer engine on a FREE scenario (ARTEL-1149).

A FREE scenario is test cases in no order, played scene by scene: in each scene the planner gets the
cases still to verify there and answers which to verify now, in what order, with shared actions and
one goal per case; the executer plays them one after another; the scene evaluator judges them in
one answer. A case put off waits for the next round, after `reset_game`; what is still left at the
limits is reported failed as UNREACHED. Same fakes as `test_planner_executer_steps.py`.
"""

import asyncio

import pytest

from app.agents.planner_executer import runner as runner_module
from app.agents.planner_executer.evaluator import CASES_PER_JUDGEMENT, SceneEvaluator
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.free import FreeCase, candidates, next_scene
from app.agents.planner_executer.planner import read_plan
from app.agents.planner_executer.prompts import case_view
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, FakeJev, _config, _judge, _menu, _verdicts


@pytest.fixture(autouse=True)
def _quick_reset(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "RESET_READING_SECONDS", 0.05)


def _case(case_id: int, scene: str | None, step: str, expected: str) -> QaStep:
    return QaStep(
        action="",
        case_id=case_id,
        case=QaCaseRef(id=case_id, scene=scene, test_step=step, expected=expected),
    )


def _free(*steps: QaStep) -> QaScenario:
    return QaScenario(title="Free", steps=list(steps), execution_mode="FREE")


MAP_CASE = _case(7, "Map", "Look at the map.", "Stage 0 is shown.")
MENU_CASE = _case(8, "Menu", "Press Start.", "The map opens.")

# `MACROS` without its `done`: the shared actions of a scene plan.
ACTIONS = MACROS.split("def done() -> bool:")[0]


def _plan(verify: list[int], later: dict | None = None, goals: dict[int, str] | None = None, actions: str = ACTIONS) -> str:
    import json

    goals = goals or {}
    body = actions + "".join(
        f"\n\ndef done_{case_id}() -> bool:\n    return {goals.get(case_id, 'scene() == \"Map\"')}\n" for case_id in verify
    )
    return (
        f"```plan\n{json.dumps({'verify': verify, 'later': later or {}})}\n```\n"
        f"```python\n{body}```"
    )


MOVE = f"```python\n{MACROS}```"


class Raw:
    """The planner's model, answering with the whole text — plan, python and developer blocks."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answers.pop(0)


def _verdicts_of(*pairs: tuple[int, bool]) -> dict:
    return {"verdicts": [{"case_id": case_id, "passed": passed, "reason": f"case {case_id} {'ok' if passed else 'no'}"} for case_id, passed in pairs]}


def _resettable_menu() -> FakeGame:
    """`_menu`, and `reset_game` takes the game back to the menu."""
    fake = _menu()

    def back_to_menu(game: FakeGame) -> None:
        game.scene = "Menu"
        game.objects = {"Root/Start": {"text": "Start"}}

    fake.reactions[("reset_game", None)] = back_to_menu
    return fake


def _run_free(fake: FakeGame, scenario: QaScenario, asked: Raw, judge, jev: FakeJev, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, scenario)

    return asyncio.run(go())


def _resets(fake: FakeGame) -> int:
    return sum(1 for method, _ in fake.actions if method == "reset_game")


# --- which cases a scene is offered, and where to go next ----------------------------------------


def test_a_scene_is_offered_its_own_cases_then_the_ones_with_no_known_scene() -> None:
    pending = [FreeCase(1, "Map"), FreeCase(2, "Menu"), FreeCase(3, None), FreeCase(4, "menu"), FreeCase(5, "Attic")]
    known = {"map", "menu"}

    assert [case.step for case in candidates(pending, "Menu", known, 10)] == [2, 4, 3, 5]
    assert [case.step for case in candidates(pending, "Menu", known, 2)] == [2, 4]
    # With no map every named scene counts as known.
    assert [case.step for case in candidates(pending, "Menu", set(), 10)] == [2, 4, 3]


def test_the_next_scene_is_the_first_written_case_of_a_known_scene_not_on_screen() -> None:
    pending = [FreeCase(3, None), FreeCase(1, "Map"), FreeCase(2, "Menu")]

    assert next_scene(pending, "Menu", {"map", "menu"}).step == 1
    assert next_scene(pending, "MAP", {"map", "menu"}).step == 2
    assert next_scene(pending[:2], "map", {"map", "menu"}) is None


# --- the plan block ------------------------------------------------------------------------------


def test_a_plan_puts_every_candidate_it_leaves_out_later() -> None:
    verify, later = read_plan('{"verify": [8], "later": {"9": "needs coins"}}', [7, 8, 9])

    assert verify == [8]
    assert later == {9: "needs coins", 7: "the plan did not include it"}


@pytest.mark.parametrize(
    ("text", "names"),
    [
        (None, "```plan block first"),
        ("not json", "not JSON"),
        ('{"verify": [99]}', "99, which are not candidates"),
        ('{"verify": [8, 8]}', "listed twice"),
        ('{"verify": [8], "later": {"8": "x"}}', "listed twice"),
    ],
)
def test_a_plan_that_cannot_be_used_is_refused_with_why(text, names) -> None:
    with pytest.raises(MacroRejection, match=names):
        read_plan(text, [7, 8])


def test_a_case_is_shown_with_its_scene_what_to_do_and_what_to_verify() -> None:
    assert case_view(_free(MAP_CASE, MENU_CASE), 2) == {
        "case_id": 8,
        "scene": "Menu",
        "do": "Press Start.",
        "verify": "The map opens.",
    }


# --- playing a FREE scenario ---------------------------------------------------------------------


def test_each_scene_is_planned_and_judged_once_for_all_its_cases() -> None:
    fake = _resettable_menu()
    asked = Raw(_plan([8]), _plan([7], actions=""))
    judge, seen = _judge(_verdicts_of((8, True)), _verdicts_of((7, True)))

    state, failure = _run_free(fake, _free(MAP_CASE, MENU_CASE), asked, judge, FakeJev())

    assert failure is None and state.finished
    # The game starts on the menu: the menu's case first; play leaves the game on the map, so the
    # map is planned next without a move.
    assert [(v["step"], v["case_id"], v["status"]) for v in _verdicts(fake)] == [(2, 8, "COMPLETED"), (1, 7, "COMPLETED")]
    assert len(asked.calls) == 2 and len(seen) == 2
    assert asked.calls[0][0].startswith("You plan the test cases of ONE scene of a game as macros.")
    assert "Scene on screen: Menu" in asked.calls[0][1] and '"case_id": 8' in asked.calls[0][1]
    assert '"case_id": 7' not in asked.calls[0][1]
    assert seen[0][0].startswith("You judge the test cases of ONE scene of a game")
    assert _resets(fake) == 0
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED" and closing["summary"]["cases"]["passed"] == 2


def test_the_planner_moves_to_a_scene_that_still_has_cases() -> None:
    fake = _resettable_menu()
    asked = Raw(MOVE, _plan([7], actions=""))
    judge, _ = _judge(_verdicts_of((7, True)))
    jev = FakeJev()

    _run_free(fake, _free(MAP_CASE), asked, judge, jev)

    # The menu has no case: the planner writes the move to the map, then plans the map.
    assert "Go to the scene `Map`." in asked.calls[0][1]
    # Jev 는 중간 씬을 거치는 것도 진행으로 읽는다(ARTEL-1189).
    (pick, *_) = jev.asked("choice")
    assert "It may take several scenes on the way" in pick["questions"]["answer"]["instructions"]
    assert asked.calls[0][0].startswith("You write ONE test step of a game as macros.")
    assert [(v["case_id"], v["status"]) for v in _verdicts(fake)] == [(7, "COMPLETED")]


def test_a_case_never_reached_is_played_again_after_a_reset() -> None:
    fake = _resettable_menu()
    jev = FakeJev(picks=("look_around", "look_around", "look_around", "press_start"))
    judge, _ = _judge(_verdicts_of((8, False)), _verdicts_of((8, True)))

    _run_free(fake, _free(MENU_CASE), Raw(_plan([8]), _plan([8])), judge, jev, max_turns_per_step=3)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and verdict["case_id"] == 8
    assert _resets(fake) == 1


def test_a_case_the_planner_keeps_for_later_is_reported_unreached_at_the_limits() -> None:
    fake = _resettable_menu()
    later = {"8": "Start needs 10 coins and the wallet is empty."}
    judge, seen = _judge()

    _run_free(fake, _free(MENU_CASE), Raw(_plan([], later), _plan([], later)), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith(
        "UNREACHED (도달 못함) DEFERRED: the planner kept leaving it for later, so it was not played — 0 plays and 1 reset."
    )
    assert "Start needs 10 coins" in verdict["message"]
    assert seen == [] and _resets(fake) == 1


def test_a_case_still_unreached_after_its_plays_is_reported_unreached() -> None:
    fake = _resettable_menu()
    judge, _ = _judge(_verdicts_of((8, False)), _verdicts_of((8, False)))

    _run_free(
        fake, _free(MENU_CASE), Raw(_plan([8]), _plan([8])), judge, FakeJev(picks=("look_around",)),
        max_turns_per_step=3,
    )

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith(
        "UNREACHED (도달 못함) GOAL_UNREACHED: play never reached the case's goal — 2 plays and 1 reset."
    )
    assert _resets(fake) == 1


def test_a_case_that_reached_its_goal_and_failed_is_reported_without_a_retry() -> None:
    fake = _resettable_menu()
    judge, _ = _judge(_verdicts_of((8, False)))

    _run_free(fake, _free(MENU_CASE), Raw(_plan([8])), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and verdict["message"] == "case 8 no"
    assert _resets(fake) == 0


def test_a_plan_missing_a_goal_is_written_again() -> None:
    fake = _resettable_menu()
    no_goal = _plan([8]).replace("def done_8() -> bool:", "def finished() -> bool:")
    asked = Raw(no_goal, _plan([8]))
    judge, _ = _judge(_verdicts_of((8, True)))

    _run_free(fake, _free(MENU_CASE), asked, judge, FakeJev())

    assert "needs `def done_8() -> bool:`" in asked.calls[1][1]
    assert [v["status"] for v in _verdicts(fake)] == ["COMPLETED"]


def test_a_plan_still_refused_fails_the_scenes_own_cases() -> None:
    fake = _resettable_menu()
    broken = "```plan\nnot json\n```"
    judge, _ = _judge()

    _run_free(fake, _free(MENU_CASE), Raw(broken, broken), judge, FakeJev(), max_rewrites=1)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith("PLANNER-REFUSED: the scene plan was still refused after 1 rewrites:")


MENU_SECOND = _case(9, "Menu", "Read the Start button.", "Start is shown.")


def test_a_scene_with_more_cases_than_one_plan_takes_is_planned_again() -> None:
    fake = _resettable_menu()
    menu_goal = {8: 'scene() == "Menu"', 9: 'scene() == "Menu"'}
    asked = Raw(_plan([8], goals=menu_goal, actions=""), _plan([9], goals=menu_goal, actions=""))
    judge, _ = _judge(_verdicts_of((8, True)), _verdicts_of((9, True)))

    _run_free(fake, _free(MENU_CASE, MENU_SECOND), asked, judge, FakeJev(), free_batch_size=1)

    # Two plans in the menu in the same round, one case each; no reset.
    assert '"case_id": 8' in asked.calls[0][1] and '"case_id": 9' not in asked.calls[0][1]
    assert '"case_id": 9' in asked.calls[1][1]
    assert [v["case_id"] for v in _verdicts(fake)] == [8, 9]
    assert _resets(fake) == 0


def test_a_case_planned_after_one_that_leaves_the_scene_is_not_played_there() -> None:
    fake = _resettable_menu()
    # Case 8 presses Start and the game goes to the map; case 9 was planned for the menu.
    plan = _plan([8, 9], goals={8: 'scene() == "Map"', 9: 'scene() == "Menu"'})
    later = _plan([9], goals={9: 'scene() == "Menu"'}, actions="")
    judge, seen = _judge(_verdicts_of((8, True)), _verdicts_of((9, True)))

    _run_free(fake, _free(MENU_CASE, MENU_SECOND), Raw(plan, MOVE.replace('"Map"', '"Menu"'), later), judge, FakeJev())

    # Case 9 is judged on its own, after the round's reset brings the menu back.
    assert '"case_id": 9' not in seen[0][1]
    assert [(v["case_id"], v["status"]) for v in _verdicts(fake)] == [(8, "COMPLETED"), (9, "COMPLETED")]
    assert "UNREACHED" not in _verdicts(fake)[1]["message"]


# --- judging a scene -----------------------------------------------------------------------------


def test_a_case_the_scene_verdict_leaves_out_is_asked_for_again_then_fails() -> None:
    judge, seen = _judge(_verdicts_of((7, True)), {"verdicts": []})
    plays = [(1, StepPlay("done", end_screen="map")), (2, StepPlay("done", end_screen="map"))]

    verdicts = asyncio.run(SceneEvaluator(_config(), judge).judge(_free(MAP_CASE, MENU_CASE), plays, "menu"))

    assert verdicts[1] == (True, "case 7 ok")
    assert not verdicts[2][0] and verdicts[2][1].startswith("EVALUATOR-ERROR: no readable verdict for the case")
    # The second question only carries the case still missing.
    assert '"case_id": 8' in seen[1][1] and '"case_id": 7' not in seen[1][1]


def test_a_scene_judgement_asks_a_few_cases_at_a_time() -> None:
    cases = [_case(100 + n, "Map", f"Case {n}.", f"Result {n}.") for n in range(7)]
    plays = [(n + 1, StepPlay("done", end_screen=f"after {n}")) for n in range(7)]
    judge, seen = _judge(
        _verdicts_of(*[(100 + n, True) for n in range(5)]),
        _verdicts_of(*[(100 + n, True) for n in range(5, 7)]),
    )

    verdicts = asyncio.run(SceneEvaluator(_config(), judge).judge(_free(*cases), plays, "start"))

    assert CASES_PER_JUDGEMENT == 5 and len(seen) == 2
    assert all(verdicts[step][0] for step in range(1, 8))
    # The second group starts from the screen the fifth case left.
    assert seen[1][1].startswith("Screen before the first case began:\nafter 4")


# --- 결정 로그(ARTEL-1186) ---------------------------------------------------------------
#
# 런이 끝난 뒤 무엇을 왜 정했는지 `qa_log` 만으로 되짚을 수 있어야 한다. stage 런 157 은 씬 하나에서 16개를
# 계획하고 play 를 0번 했는데 그 까닭이 어디에도 없었다.


def _decisions(fake: FakeGame, kind: str) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == kind]


def test_a_scene_plan_is_logged_with_what_it_verifies_and_what_it_leaves_for_later() -> None:
    fake = _resettable_menu()
    later = {"8": "Start needs 10 coins and the wallet is empty."}
    judge, _ = _judge()

    _run_free(fake, _free(MENU_CASE), Raw(_plan([], later), _plan([], later)), judge, FakeJev())

    plan = _decisions(fake, "scene_plan")[0]
    assert plan["message"].startswith("Scene Menu: verify 0 of 1 cases now")
    assert plan["decision"]["later"] == {"1": "Start needs 10 coins and the wallet is empty."}
    assert plan["decision"]["actions"] and "def " in plan["decision"]["source"]
    assert [d["decision"]["round"] for d in _decisions(fake, "free_round")] == [1, 2]
    assert len(_decisions(fake, "reset")) == 1
    (end,) = _decisions(fake, "free_end")
    assert end["decision"]["causes"] == {"DEFERRED": 1}


def test_a_refused_scene_plan_is_logged_with_the_refusal() -> None:
    fake = _resettable_menu()
    no_goal = _plan([8]).replace("def done_8() -> bool:", "def finished() -> bool:")
    judge, _ = _judge(_verdicts_of((8, True)))

    _run_free(fake, _free(MENU_CASE), Raw(no_goal, _plan([8])), judge, FakeJev())

    (refused,) = _decisions(fake, "scene_plan_refused")
    assert refused["message"].startswith("Scene Menu: the plan was refused (answer 1 of 3):")
    assert "done_8" in refused["decision"]["refusal"]
    assert refused["decision"]["answer"] == 1


def test_a_move_is_logged_as_a_travel_with_where_it_went() -> None:
    fake = _resettable_menu()
    asked = Raw(MOVE, _plan([7], actions=""))
    judge, _ = _judge(_verdicts_of((7, True)))

    _run_free(fake, _free(MAP_CASE), asked, judge, FakeJev())

    (macros,) = _decisions(fake, "travel_macros")
    assert macros["message"].startswith("Travel to `Map` (for step 1):")
    (travel,) = _decisions(fake, "travel")
    assert travel["decision"]["origin"] == "Menu" and travel["decision"]["target"] == "Map"
    assert travel["decision"]["reached"] is True
    notes = [f["payload"]["message"] for f in fake.frames("LOG")]
    assert not any(note.startswith("Step 1: the planner is writing macros") for note in notes)


def test_a_move_that_does_not_arrive_is_logged_and_its_cases_end_scene_unreached() -> None:
    fake = _resettable_menu()
    judge, _ = _judge()

    _run_free(
        fake, _free(MAP_CASE), Raw(MOVE, MOVE), judge, FakeJev(picks=("look_around",)), max_turns_per_step=2
    )

    travels = _decisions(fake, "travel")
    assert [t["decision"]["reached"] for t in travels] == [False, False]
    assert travels[0]["decision"]["deferred"] == [1]
    assert travels[0]["message"].startswith("Travel Menu → Map: not reached, ended in Menu")
    (verdict,) = _verdicts(fake)
    assert verdict["message"].startswith(
        "UNREACHED (도달 못함) SCENE_UNREACHED: play could not reach the case's scene, so it was not played"
    )


def test_a_move_out_of_a_planned_scene_shows_the_planner_that_scene_plans_actions() -> None:
    """씬 계획은 case 글을 읽고 그 씬의 조작을 쓴다. 이동 계획은 그것을 몰라 전투 조작을 짐작했다(로컬 try 177)."""
    fake = _resettable_menu()
    asked = Raw(_plan([], {"8": "Later."}), MOVE, _plan([7], actions=""))
    judge, _ = _judge(_verdicts_of((7, True)))

    _run_free(fake, _free(MENU_CASE, MAP_CASE), asked, judge, FakeJev(), free_max_resets=0)

    travel_request = asked.calls[1][1]
    assert "Actions the plan for the scene on screen (`Menu`) wrote earlier in this run." in travel_request
    assert "def press_start() -> None:" in travel_request


def test_the_engine_reports_each_phase_once_when_it_changes() -> None:
    """계획·실행·판정이 바뀔 때만 `phase` 를 실은 LOG 를 보낸다. SDK 하단 줄의 "생각 중…" 이 이것을 읽는다."""
    fake = _resettable_menu()
    judge, _ = _judge(_verdicts_of((8, True)))

    _run_free(fake, _free(MENU_CASE), Raw(_plan([8])), judge, FakeJev(), free_max_resets=0)

    phases = [f["payload"]["phase"] for f in fake.frames("LOG") if "phase" in f["payload"]]
    assert phases == ["PLANNING", "EXECUTING", "EVALUATING"]
