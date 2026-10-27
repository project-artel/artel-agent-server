"""The Evaluator's failure analysis: what a failed step's `STATUS.analysis` and `ISSUE` carry.

The runner tests reuse `tests/test_planner_executer_steps.py`'s fakes: `FakeGame` answers the
engine's frames, `Asked` is the planner's model, `_judge` is the one model the Evaluator's three
parts share — so its answers are consumed in order: verdict, then (for a failed step) analysis,
then the run review at the end.
"""

import asyncio

from app.agents.evaluator import (
    FailureAnalyzer,
    FailureCategory,
    analysis_request,
    engine_error,
    sent_inputs,
    unreached,
)
from app.agents.planner_executer.executer import StepPlay
from tests.test_planner_executer_free import (
    MENU_CASE,
    Raw,
    _free,
    _plan,
    _resettable_menu,
    _run_free,
)
from tests.test_planner_executer_steps import (
    MACROS,
    Asked,
    FakeJev,
    _config,
    _judge,
    _menu,
    _run,
    _scenario,
    _verdicts,
)

REVIEW = {"overview": "One step, one failure.", "patterns": [], "insights": ["Check the start button."]}
DEFECT = {
    "category": "GAME_DEFECT",
    "cause": "Start was pressed and taken, and the menu stayed.",
    "reproduction": [1],
    "actual": "The menu is still shown.",
    "insight": "Look at what the Start button's handler does.",
    "title": "Start does not open the map",
    "severity": "MAJOR",
}

# A play that failed: the first input was refused, the second taken, the goal never held.
FAILED_PLAY = StepPlay(
    "3 actions failed in a row (last: cast_card: SELECTOR_NOT_FOUND)",
    turns=[
        {
            "step": 1, "turn": 0, "scene": "Battle", "done": False, "picked": "cast_card", "outcome": "failed",
            "reason": "SELECTOR_NOT_FOUND at 3: no object Hand/Card[0]",
            "sent": [{"summary": "drag(card, zone)", "ok": False, "error": "Nothing at selector: Hand/Card[0]", "reached": None, "blocked": None}],
        },
        {
            "step": 1, "turn": 1, "scene": "Battle", "done": False, "picked": "end_turn", "outcome": "passed",
            "sent": [{"summary": "click(end_turn)", "ok": True, "error": None, "reached": "Root/EndTurn", "blocked": None}],
        },
    ],
    start_screen="(battle, hand 3)",
    end_screen="(battle, hand 3, enemy hp 10)",
)


def _issues(fake) -> list[dict]:
    return [f["payload"] for f in fake.frames("ISSUE")]


def _reviews(fake) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "run_review"]


# --- through the runner ----------------------------------------------------------------------------


def test_a_failed_step_is_analysed_and_a_game_defect_becomes_an_issue() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": False, "reason": "The menu is still shown."}, DEFECT, REVIEW)

    state, failure = _run(fake, Asked(MACROS), judge, FakeJev())

    assert failure is None and state.finished
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and verdict["message"] == "The menu is still shown."
    analysis = verdict["analysis"]
    assert analysis["category"] == "GAME_DEFECT" and analysis["title"] == "Start does not open the map"
    assert analysis["severity"] == "MAJOR" and analysis["insight"] == DEFECT["insight"]
    # The reproduction is the sentence the macro runner sent, not the model's number.
    assert analysis["reproduction"] == ["click(start)"]
    # `done()` held on the map, so the engine claimed the goal; the Evaluator disagreed.
    assert analysis["engine_claimed"] is True
    (issue,) = _issues(fake)
    assert issue["title"] == "Start does not open the map" and issue["severity"] == "MAJOR"
    assert issue["step"] == 1 and issue["expected"] == "The map opens." and issue["actual"] == "The menu is still shown."
    assert issue["reproduction"] == ["click(start)"]
    assert issue["category"] == "GAME_DEFECT" and issue["cause"] == DEFECT["cause"] and issue["insight"] == DEFECT["insight"]
    # The analysis read the verdict and the numbered inputs.
    assert seen[1][0].startswith("You analyse ONE failed test step of a game after it was played.")
    assert "The evaluator's verdict: FAILED — The menu is still shown." in seen[1][1]
    assert "1. [turn 0, press_start] click(start) — ok" in seen[1][1]


def test_a_failure_that_is_not_a_defect_stays_on_the_status() -> None:
    fake = _menu()
    inconclusive = {**DEFECT, "category": "INCONCLUSIVE", "title": None, "severity": None}
    judge, _ = _judge({"passed": False, "reason": "Cannot tell."}, inconclusive, REVIEW)

    _run(fake, Asked(MACROS), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["analysis"]["category"] == "INCONCLUSIVE"
    assert "title" not in verdict["analysis"] and "severity" not in verdict["analysis"]
    assert _issues(fake) == []


def test_a_passed_step_is_not_analysed_and_its_status_has_no_analysis() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map scene shows Stage 0."}, REVIEW)

    _run(fake, Asked(MACROS), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and "analysis" not in verdict
    assert _issues(fake) == []
    # Verdict, then the run review. No analysis call in between.
    assert [system[:10] for system, _ in seen] == ["You judge ", "You review"]


def test_analysis_off_asks_nothing_more_and_adds_no_key() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": False, "reason": "The menu is still shown."})

    _run(fake, Asked(MACROS), judge, FakeJev(), analysis="off")

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and "analysis" not in verdict
    assert len(seen) == 1 and _issues(fake) == [] and _reviews(fake) == []
    assert "analysis" not in fake.frames("STATUS")[-1]["payload"]


def test_a_refused_step_is_filed_as_an_agent_error_without_a_model_call() -> None:
    fake = _menu()
    no_goal = MACROS.replace("def done() -> bool:", "def finished() -> bool:")
    judge, seen = _judge(REVIEW)

    _run(fake, Asked(no_goal, no_goal), judge, FakeJev(), max_rewrites=1)

    (verdict,) = _verdicts(fake)
    analysis = verdict["analysis"]
    assert analysis["category"] == "AGENT_ERROR" and analysis["cause"].startswith("PLANNER-REFUSED")
    assert analysis["reproduction"] == [] and analysis["engine_claimed"] is False
    assert _issues(fake) == []
    # Only the run review asked the model.
    assert [system[:10] for system, _ in seen] == ["You review"]


def test_an_unreached_free_case_carries_an_unreached_analysis() -> None:
    fake = _resettable_menu()
    later = {"8": "Start needs 10 coins and the wallet is empty."}
    judge, seen = _judge(REVIEW)

    _run_free(fake, _free(MENU_CASE), Raw(_plan([], later), _plan([], later)), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    analysis = verdict["analysis"]
    assert analysis["category"] == "UNREACHED"
    assert analysis["cause"].startswith("UNREACHED (도달 못함) DEFERRED")
    assert analysis["reproduction"] == [] and _issues(fake) == []
    assert [system[:10] for system, _ in seen] == ["You review"]


# --- the analyzer on its own ------------------------------------------------------------------------


def test_the_analysis_request_numbers_the_inputs_the_play_sent() -> None:
    inputs = sent_inputs(FAILED_PLAY)

    assert [item["n"] for item in inputs] == [1, 2]
    user = analysis_request(_scenario(), 1, FAILED_PLAY, "The hand did not change.", inputs)
    assert "Inputs the play sent to the game, in order (2)." in user
    assert "1. [turn 0, cast_card] drag(card, zone) — REFUSED: Nothing at selector: Hand/Card[0]" in user
    assert "2. [turn 1, end_turn] click(end_turn) — ok" in user
    assert "Why play stopped: 3 actions failed in a row" in user
    assert "Screen when the step began:\n(battle, hand 3)" in user


def test_a_refused_input_is_marked_in_the_reproduction() -> None:
    answer = {**DEFECT, "reproduction": [1, 2]}
    judge, _ = _judge(answer)

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))

    assert report.reproduction == (
        "drag(card, zone) — the game refused it: Nothing at selector: Hand/Card[0]",
        "click(end_turn)",
    )
    assert report.engine_claimed is False


def test_a_reproduction_number_outside_the_sent_inputs_is_asked_again() -> None:
    judge, seen = _judge({**DEFECT, "reproduction": [5]}, {**DEFECT, "reproduction": [2]})

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))

    assert len(seen) == 2 and report.reproduction == ("click(end_turn)",)


def test_a_game_defect_without_a_title_or_severity_is_asked_again() -> None:
    judge, seen = _judge({**DEFECT, "title": None}, {**DEFECT, "severity": None}, DEFECT)

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))

    # Two refusals use up the attempts; the third answer is never read.
    assert len(seen) == 2
    assert report.category is FailureCategory.AGENT_ERROR
    assert report.cause.startswith("ANALYSIS-ERROR: no readable analysis in 2 answers")
    assert not report.is_defect
    # What the play sent is still there to read, refused or not.
    assert len(report.reproduction) == 2


def test_an_engine_stop_is_filed_without_asking() -> None:
    judge, seen = _judge()
    stopped = StepPlay("JEV-ERROR: decisions endpoint down", turns=FAILED_PLAY.turns, start_screen="a", end_screen="b")

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, stopped, "The hand did not change."))

    assert seen == [] and report.category is FailureCategory.AGENT_ERROR
    assert report.cause == "JEV-ERROR: decisions endpoint down"
    assert len(report.reproduction) == 2

    judged_badly = asyncio.run(
        FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "EVALUATOR-ERROR: no readable verdict")
    )
    assert seen == [] and judged_badly.cause.startswith("EVALUATOR-ERROR")


def test_code_filled_reports_have_the_same_shape_as_the_model_s() -> None:
    error = engine_error("PLANNER-REFUSED: no `done`")
    missed = unreached("UNREACHED (도달 못함) SCENE_UNREACHED: play could not reach the case's scene", FAILED_PLAY)

    assert set(error.as_payload()) == {"category", "cause", "reproduction", "actual", "insight", "engine_claimed"}
    assert error.as_payload()["category"] == "AGENT_ERROR" and error.reproduction == ()
    assert missed.as_payload()["category"] == "UNREACHED" and len(missed.reproduction) == 2
    assert not error.is_defect and not missed.is_defect
