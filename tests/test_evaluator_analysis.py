"""The Evaluator's failure analysis: what a failed step's `STATUS.analysis` carries, and how the
reproduction is held to the inputs the play really sent.

The runner tests reuse `tests/test_planner_executer_steps.py`'s fakes: `FakeGame` answers the
engine's frames, `Asked` is the planner's model, `_judge` is the one model the Evaluator's three
parts share — so its answers are consumed in order: verdict, then (for a failed step) analysis,
then the run review at the end.
"""

import asyncio

import pytest

from app.agents.evaluator import (
    FailureAnalyzer,
    FailureCategory,
    analysis_request,
    engine_error,
    input_line,
    sent_inputs,
    unreached,
)
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.macro_host import describe_actions, object_name
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.qa.envelope import JsonRpcAction
from tests.test_planner_executer_free import MENU_CASE, Raw, _free, _plan, _resettable_menu, _run_free
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
    "cause": "Start 를 눌렀고 게임이 받았는데 메뉴가 그대로다.",
    "reproduction": [{"n": 1, "text": "타이틀 화면에서 Start 버튼을 누른다."}],
    "actual": "메뉴가 그대로 보인다.",
    "insight": "Start 버튼의 handler 가 무엇을 하는지 본다.",
    "title": "Start 가 지도를 열지 않는다",
    "severity": "MAJOR",
}


def _batch(**fields) -> dict:
    """One sent batch as `ChannelMacroHost` records it, with every key present."""
    base = {
        "summary": "", "ok": True, "error": None, "reached": None, "blocked": None,
        "kind": "other", "scene": None, "target": None, "target_name": None,
        "target_text": None, "to_text": None, "key": None, "value": None,
    }
    return {**base, **fields}


# A play that failed: a refused drag, a key press, a click that landed on a text-less sprite.
FAILED_PLAY = StepPlay(
    "3 actions failed in a row (last: cast_card: SELECTOR_NOT_FOUND)",
    turns=[
        {
            "step": 1, "turn": 0, "scene": "Battle", "done": False, "picked": "cast_card", "outcome": "failed",
            "reason": "SELECTOR_NOT_FOUND at 3: no object Hand/Card[0]",
            "sent": [_batch(
                summary="drag(card, zone)", ok=False, error="Nothing at selector: Hand/Card[0]", kind="drag",
                scene="Battle", target="Root/Hand/Card(Clone)[0]", target_name="Card", target_text="Fireball",
                to_text="PlayZone",
            )],
        },
        {
            "step": 1, "turn": 1, "scene": "Battle", "done": False, "picked": "end_turn", "outcome": "passed",
            "sent": [_batch(
                summary='press_key("Space", 0.1)', kind="key", scene="Battle", key="Space", reached="nothing",
            )],
        },
        {
            "step": 1, "turn": 2, "scene": "Battle", "done": False, "picked": "poke_enemy", "outcome": "passed",
            "sent": [_batch(
                summary="click(enemy)", kind="click", scene="Battle", target="Root/Enemies/Goblin(Clone)[2]",
                target_name="Goblin", reached="Root/Enemies/Goblin(Clone)[2]",
            )],
        },
    ],
    start_screen="(battle, hand 3)",
    end_screen="(battle, hand 3, enemy hp 10)",
)
GOOD_LINES = [
    {"n": 1, "text": "전투 화면에서 Fireball 카드를 PlayZone 으로 끌어다 놓는다."},
    {"n": 2, "text": "Space 키를 누른다."},
    {"n": 3, "text": "오른쪽 둘째 고블린을 누른다."},
]


def _analysis(answer: dict):
    judge, seen = _judge(answer)
    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))
    return report, seen


def _reviews(fake) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "run_review"]


# --- through the runner ----------------------------------------------------------------------------


def test_a_failed_step_is_analysed_and_the_analysis_rides_the_status() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": False, "reason": "The menu is still shown."}, DEFECT, REVIEW)

    state, failure = _run(fake, Asked(MACROS), judge, FakeJev())

    assert failure is None and state.finished
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and verdict["message"] == "The menu is still shown."
    analysis = verdict["analysis"]
    assert analysis["category"] == "GAME_DEFECT" and analysis["title"] == "Start 가 지도를 열지 않는다"
    assert analysis["severity"] == "MAJOR" and analysis["insight"] == DEFECT["insight"]
    # The sentence is the model's; the source beside it is what the macro host recorded.
    assert analysis["reproduction"] == ["타이틀 화면에서 Start 버튼을 누른다."]
    (source,) = analysis["reproduction_inputs"]
    assert source == {
        "n": 1, "scene": "Menu", "kind": "click", "target_text": "Start", "target_name": "Start",
        "to_text": None, "key": None, "value": None, "result": "ok", "source": "click(start)",
    }
    # `done()` held on the map, so the engine claimed the goal; the Evaluator disagreed.
    assert analysis["engine_claimed"] is True
    # No ISSUE: a person approves a failed case into an issue.
    assert fake.frames("ISSUE") == []
    # The frames and the clock the play ran between, for the recording to be cut by.
    played = verdict["played"]
    assert set(played) == {
        "start_frame", "end_frame", "started_at", "ended_at", "seconds", "stopped", "end_state_version"
    }
    assert played["start_frame"] <= played["end_frame"] and played["stopped"] == "done"
    assert played["started_at"].endswith("Z") and played["ended_at"] >= played["started_at"]
    # The analysis read the verdict, the numbered inputs with their on-screen text, and the directive.
    system, user = seen[1]
    assert system.startswith("You analyse ONE failed test step of a game after it was played.")
    assert LANGUAGE_DIRECTIVES[_config().language] in system
    assert "The evaluator's verdict: FAILED — The menu is still shown." in user
    assert '1. [Menu] click "Start" — ok  (source: click(start))' in user


def test_a_failure_that_is_not_a_defect_has_no_title() -> None:
    fake = _menu()
    inconclusive = {**DEFECT, "category": "INCONCLUSIVE", "title": None, "severity": None}
    judge, _ = _judge({"passed": False, "reason": "Cannot tell."}, inconclusive, REVIEW)

    _run(fake, Asked(MACROS), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["analysis"]["category"] == "INCONCLUSIVE"
    assert "title" not in verdict["analysis"] and "severity" not in verdict["analysis"]


def test_a_passed_step_is_not_analysed_and_its_status_has_no_analysis() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map scene shows Stage 0."}, REVIEW)

    _run(fake, Asked(MACROS), judge, FakeJev())

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and "analysis" not in verdict
    # Verdict, then the run review. No analysis call in between.
    assert [system[:10] for system, _ in seen] == ["You judge ", "You review"]


def test_analysis_off_asks_nothing_more_and_adds_no_key() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": False, "reason": "The menu is still shown."})

    _run(fake, Asked(MACROS), judge, FakeJev(), analysis="off")

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and "analysis" not in verdict
    assert len(seen) == 1 and _reviews(fake) == []
    assert "analysis" not in fake.frames("STATUS")[-1]["payload"]


def test_a_refused_step_is_filed_as_an_agent_error_without_a_model_call() -> None:
    fake = _menu()
    no_goal = MACROS.replace("def done() -> bool:", "def finished() -> bool:")
    judge, seen = _judge(REVIEW)

    _run(fake, Asked(no_goal, no_goal), judge, FakeJev(), max_rewrites=1)

    (verdict,) = _verdicts(fake)
    analysis = verdict["analysis"]
    assert analysis["category"] == "AGENT_ERROR" and analysis["cause"].startswith("PLANNER-REFUSED")
    assert analysis["reproduction"] == [] and analysis["reproduction_inputs"] == []
    assert analysis["engine_claimed"] is False
    # Never played, so nothing to cut a clip from.
    assert "played" not in verdict
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
    assert analysis["reproduction"] == []
    assert [system[:10] for system, _ in seen] == ["You review"]


# --- what the macro host records about each input ----------------------------------------------------


class _Pulse:
    scene = "Battle"

    def __init__(self, **objects) -> None:
        self.held = {
            selector: type("Obj", (), {"selector": selector, "path": selector, "id": None, "text": text})()
            for selector, text in objects.items()
        }


def test_the_host_describes_a_click_by_the_text_on_the_object() -> None:
    pulse = _Pulse(**{"Root/Start": "Start", "Root/Hand/Card(Clone)[0]": "", "Root/Zone": "PlayZone"})
    click = [JsonRpcAction(id=1, method="move_mouse", params=["Root/Start"]),
             JsonRpcAction(id=2, method="mouse_down", params=[0]), JsonRpcAction(id=3, method="mouse_up", params=[0])]
    drag = [JsonRpcAction(id=1, method="move_mouse", params=["Root/Hand/Card(Clone)[0]"]),
            JsonRpcAction(id=2, method="mouse_down", params=[0]),
            JsonRpcAction(id=3, method="move_mouse", params=["Root/Zone"]), JsonRpcAction(id=4, method="mouse_up", params=[0])]
    key = [JsonRpcAction(id=1, method="key_click", params=["Space", 0.1])]
    typed = [JsonRpcAction(id=1, method="enter_text", params=[4242, "hello"])]

    assert describe_actions(click, pulse) == {
        "kind": "click", "scene": "Battle", "target": "Root/Start", "target_name": "Start",
        "target_text": "Start", "to_text": None, "key": None, "value": None,
    }
    dragged = describe_actions(drag, pulse)
    # A sprite with no text keeps its name; the destination has text.
    assert dragged["kind"] == "drag" and dragged["target_text"] is None and dragged["target_name"] == "Card"
    assert dragged["to_text"] == "PlayZone"
    assert describe_actions(key, pulse)["kind"] == "key" and describe_actions(key, pulse)["key"] == "Space"
    assert describe_actions(typed, pulse)["kind"] == "text" and describe_actions(typed, pulse)["value"] == "hello"
    assert object_name("Root/Enemies/Goblin(Clone)[2]") == "Goblin" and object_name(503402) is None


def test_the_analysis_request_lists_each_input_as_a_player_saw_it() -> None:
    inputs = sent_inputs(FAILED_PLAY)

    assert [item["n"] for item in inputs] == [1, 2, 3]
    assert input_line(inputs[0]) == (
        '  1. [Battle] drag "Fireball" to "PlayZone" — refused: Nothing at selector: Hand/Card[0]  (source: drag(card, zone))'
    )
    assert input_line(inputs[1]) == '  2. [Battle] press key "Space" — landed on nothing  (source: press_key("Space", 0.1))'
    assert input_line(inputs[2]) == "  3. [Battle] click an object named Goblin with no text on it — ok  (source: click(enemy))"
    user = analysis_request(_scenario(), 1, FAILED_PLAY, "The hand did not change.", inputs)
    assert "Inputs the play sent to the game, in order (3)." in user
    assert "Screen when the step began:\n(battle, hand 3)" in user


# --- holding the sentences to their sources --------------------------------------------------------------


def test_sentences_bound_to_their_inputs_pass_and_keep_their_sources() -> None:
    report, seen = _analysis({**DEFECT, "reproduction": GOOD_LINES})

    assert len(seen) == 1
    assert report.reproduction == tuple(line["text"] for line in GOOD_LINES)
    assert [item["n"] for item in report.inputs] == [1, 2, 3]
    assert report.as_payload()["reproduction_inputs"][0]["result"].startswith("refused:")
    assert report.as_payload()["reproduction_inputs"][1]["result"] == "landed on nothing"
    assert report.engine_claimed is False


@pytest.mark.parametrize(
    "bad, why",
    [
        ([{"n": 5, "text": "다섯 번째를 누른다."}], "a number the play never sent"),
        ([GOOD_LINES[1], GOOD_LINES[1]], "the same number twice"),
        ([{"n": 1, "text": "카드를 PlayZone 으로 끌어다 놓는다."}], "the text the input landed on is missing"),
        ([{"n": 1, "text": "Fireball 카드를 끌어다 놓는다."}], "the drag destination is missing"),
        ([{"n": 2, "text": "키를 누른다."}], "the key is missing"),
        ([{"n": 3, "text": "Goblin(Clone)[2] 를 누른다."}], "a prefab name a player cannot see"),
        ([{"n": 3, "text": "Root/Enemies/Goblin(Clone)[2] 를 누른다."}], "a selector path"),
        ([{"n": 1, "text": "drag(card, zone) 을 실행한다."}], "a macro call"),
        ([{"n": 1, "text": "   "}], "an empty sentence"),
    ],
)
def test_a_sentence_not_held_by_its_source_is_asked_again(bad, why) -> None:
    judge, seen = _judge({**DEFECT, "reproduction": bad}, {**DEFECT, "reproduction": GOOD_LINES})

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))

    assert len(seen) == 2, why
    assert report.reproduction == tuple(line["text"] for line in GOOD_LINES)


def test_on_screen_text_is_matched_without_regard_to_case() -> None:
    report, seen = _analysis({**DEFECT, "reproduction": [{"n": 2, "text": "space 키를 누른다."}]})

    assert len(seen) == 1 and report.reproduction == ("space 키를 누른다.",)


def test_a_game_defect_without_a_title_or_severity_is_asked_again() -> None:
    judge, seen = _judge({**DEFECT, "title": None}, {**DEFECT, "severity": None}, DEFECT)

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "failed"))

    # Two refusals use up the attempts; the third answer is never read.
    assert len(seen) == 2
    assert report.category is FailureCategory.AGENT_ERROR
    assert report.cause.startswith("ANALYSIS-ERROR: no readable analysis in 2 answers")
    assert not report.is_defect and report.reproduction == ()
    # What the play sent is still there to read.
    assert len(report.inputs) == 3


def test_an_engine_stop_is_filed_without_asking() -> None:
    judge, seen = _judge()
    stopped = StepPlay("JEV-ERROR: decisions endpoint down", turns=FAILED_PLAY.turns, start_screen="a", end_screen="b")

    report = asyncio.run(FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, stopped, "The hand did not change."))

    assert seen == [] and report.category is FailureCategory.AGENT_ERROR
    assert report.cause == "JEV-ERROR: decisions endpoint down"
    assert report.reproduction == () and len(report.inputs) == 3

    judged_badly = asyncio.run(
        FailureAnalyzer(_config(), judge).analyze(_scenario(), 1, FAILED_PLAY, "EVALUATOR-ERROR: no readable verdict")
    )
    assert seen == [] and judged_badly.cause.startswith("EVALUATOR-ERROR")


def test_code_filled_reports_have_the_same_shape_as_the_model_s() -> None:
    error = engine_error("PLANNER-REFUSED: no `done`")
    missed = unreached("UNREACHED (도달 못함) SCENE_UNREACHED: play could not reach the case's scene", FAILED_PLAY)

    assert set(error.as_payload()) == {
        "category", "cause", "reproduction", "reproduction_inputs", "actual", "insight", "engine_claimed"
    }
    assert error.as_payload()["category"] == "AGENT_ERROR" and error.inputs == ()
    assert missed.as_payload()["category"] == "UNREACHED" and len(missed.inputs) == 3
    assert not error.is_defect and not missed.is_defect
