"""The Evaluator's run review: what the terminal `STATUS.analysis` and the `run_review` LOG carry."""

import asyncio

from app.agents.evaluator import RunReviewer, engine_error, review_request
from app.agents.qa.tools import QaRunState
from app.qa.schemas import QaCaseRef, QaScenario, QaStep, QaStepResult
from tests.test_planner_executer_steps import (
    MACROS,
    Asked,
    FakeJev,
    _config,
    _judge,
    _menu,
    _run,
    _scenario,
)

PASSED = {"passed": True, "reason": "The map scene shows Stage 0."}
REVIEW = {
    "overview": "1 of 1 steps passed. Nothing failed, so there is nothing to fix.",
    "patterns": [],
    "insights": [],
}


def _reviews(fake) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "run_review"]


def test_the_run_review_rides_the_terminal_status_and_one_log_line() -> None:
    fake = _menu()
    judge, seen = _judge(PASSED, REVIEW)

    state, failure = _run(fake, Asked(MACROS), judge, FakeJev())

    assert failure is None and state.finished
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED"
    assert closing["analysis"] == REVIEW
    (line,) = _reviews(fake)
    assert line["message"] == REVIEW["overview"] and line["category"] == "THOUGHT"
    assert line["decision"] == {"patterns": [], "insights": []}
    # The review reads the scenario and every step's verdict.
    system, user = seen[1]
    assert system.startswith("You review ONE QA run of a game after every step was judged.")
    assert "Scenario: Start" in user and "Result: 1 of 1 steps passed." in user
    assert "step 1 (case 5): Press Start. → verify: The map opens.\n  PASSED: The map scene shows Stage 0." in user


def test_two_unreadable_reviews_leave_the_terminal_status_without_analysis() -> None:
    fake = _menu()
    judge, seen = _judge(PASSED, "no json here", "still none")

    _run(fake, Asked(MACROS), judge, FakeJev())

    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED" and "analysis" not in closing
    assert _reviews(fake) == [] and len(seen) == 3


def test_the_review_request_lists_each_step_with_its_verdict_and_analysis() -> None:
    scenario = QaScenario(
        title="Battle",
        steps=[
            QaStep(action="Enter the battle.", case_id=3, case=QaCaseRef(id=3, scene="Map", expected="The battle starts.")),
            QaStep(action="Cast a card.", case_id=3, case=QaCaseRef(id=3, scene="Map", expected="The battle starts.")),
            QaStep(action="Open the settings.", case_id=None),
        ],
    )
    state = QaRunState(total_steps=3, step_meta=[(3, False), (3, True), (None, False)])
    state.step_results.extend(
        [
            QaStepResult(step=1, passed=True, message="The battle scene is shown.", case_id=3),
            QaStepResult(step=2, passed=False, message="PLANNER-REFUSED: no `done`", case_id=3, is_verification=True),
        ]
    )
    reports = {2: engine_error("PLANNER-REFUSED: no `done`")}

    user = review_request(scenario, state, reports)

    assert "Scenario: Battle\nMode: ORDERED\nResult: 1 of 3 steps passed." in user
    # The first step of a case does not verify its expected result; the last one does.
    assert "step 1 (case 3, scene Map): Enter the battle.\n  PASSED: The battle scene is shown." in user
    assert "step 2 (case 3, scene Map): Cast a card. → verify: The battle starts.\n  FAILED: PLANNER-REFUSED: no `done`" in user
    assert "  analysis: AGENT_ERROR — PLANNER-REFUSED: no `done`" in user
    assert "step 3: Open the settings.\n  NOT REPORTED" in user


def test_long_reviews_are_trimmed_to_what_a_terminal_frame_carries() -> None:
    judge, _ = _judge(
        {"overview": "x" * 3_000, "patterns": [f"pattern {n}" for n in range(12)], "insights": ["y" * 600]}
    )
    state = QaRunState(total_steps=1, step_meta=[(5, True)])

    review = asyncio.run(RunReviewer(_config(), judge).review(_scenario(), state, {}))

    assert review is not None
    assert len(review.overview) < 2_100 and review.overview.endswith("(+1000 chars)")
    assert len(review.patterns) == 10 and review.patterns[-1] == "pattern 9"
    assert review.insights[0].endswith("(+100 chars)")
