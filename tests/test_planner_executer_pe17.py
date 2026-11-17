"""pe17: 다시 쓴 계획이 묶음을 이어 받는다.

- 다시 쓴 답이 ```segment 를 보내지 않으면 지금 계획의 묶음을 이어 받고, 뒤 step 은 그 계획의 몫으로 돈다.
- 묶음 중간 step 에서 다시 쓰면 지나간 줄은 버리고 그 step 은 `done_<step>()` 으로 끝난다.
- ```changes 의 replace·remove 는 action 말고 계획의 다른 `def` 도 받는다.
- 시작 검사는 묶음 계획이면 그 step 이 맡은 action 만 센다.
"""

import asyncio

import pytest

from app.agents.planner_executer.executer import LATER_DONE, RESULT_IN_EARLIER_STEP
from app.agents.planner_executer.macros import RegisteredMacros, read_segment
from app.agents.planner_executer.plan_edit import PlanDraft, merge, read_changes
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.test_planner_executer_pe11 import LATER_ANSWER, LATER_SOURCE
from tests.test_planner_executer_steps import MACROS, FakeJev, _config, _menu, _verdicts


def _run(fake, ask, jev, scenario, **knobs):
    async def judge(system: str, user: str):
        return {"passed": True, "reason": "ok", "state_detail": "", "game_insight": "", "checks": [], "precondition_held": None}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off", **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=ask, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, scenario)

    return asyncio.run(go())


def _logs(fake, kind: str) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == kind]


# --- 다시 쓴 계획이 묶음을 이어 받는다 ----------------------------------------------------------------


def _story_then_map_with_precondition() -> QaScenario:
    return QaScenario(
        title="Story then map",
        steps=[
            QaStep(
                action="Read the story.", case_id=5,
                case=QaCaseRef(id=5, precondition="No window covers the menu.", expected="The story goes on."),
            ),
            QaStep(action="Reach the map.", case_id=6, case=QaCaseRef(id=6, expected="The map opens.")),
        ],
    )


def test_a_plan_written_again_for_its_precondition_keeps_the_segment_and_the_later_step_plays_on_it() -> None:
    """로컬 try 36·37 step 16 의 모양: 묶음 계획을 사전 조건 때문에 다시 쓰자 묶음만 사라지고 뒤 step 의 action 이 남았다."""
    fake = _menu()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return LATER_ANSWER
        return '```changes\n{"base": 1, "replace": {}, "remove": {}, "add": []}\n```'

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",), goal=(0.1,)), _story_then_map_with_precondition())

    assert failure is None and state.finished
    assert len(calls) == 2, "step 2 는 다시 쓴 계획의 몫으로 돈다"
    (replanned,) = _logs(fake, "replan_macros")
    assert replanned["decision"]["segment"] == {
        "1": {"goals": ["open_map"], "actions": ["press_start", "look_around"]},
        "2": {"goals": ["stay"], "actions": ["look_around"]},
    }
    assert [verdict["played"]["stopped"] for verdict in _verdicts(fake)] == [LATER_DONE, RESULT_IN_EARLIER_STEP]
    assert [f["payload"]["step"] for f in fake.frames("LOG") if f["payload"].get("kind") == "segment_step"] == [2]
    assert "```segment" in calls[1], "다시 쓰는 요청에 지금 계획의 묶음이 보인다"


def test_a_segment_written_again_without_what_it_names_is_refused_with_a_hint() -> None:
    fake = _menu()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        if len(calls) == 1:
            return LATER_ANSWER
        return '```changes\n{"base": 1, "replace": {}, "remove": {"look_around": "not needed"}, "add": []}\n```'

    _run(fake, ask, FakeJev(picks=("press_start",), goal=(0.1,)), _story_then_map_with_precondition(), max_rewrites=0)

    (refused,) = _logs(fake, "replan_macros_refused")
    assert "`look_around`" in refused["message"]
    assert "kept from the plan you changed" in refused["decision"]["refusal"]


# --- 묶음 중간 step 에서 다시 쓰기 -----------------------------------------------------------------------

THREE_STEPS = (
    LATER_SOURCE
    + '\n\ndef done_3() -> bool:\n    return scene() == "Map"\n'
)
THREE_ROWS = "1: open_map | press_start\n2: stay | look_around\n3: stay | look_around\n"


def test_a_segment_kept_from_an_earlier_step_drops_the_rows_already_played_and_ends_the_step_on_its_own_done() -> None:
    async def go():
        fake = _menu()
        segment = await read_segment(
            THREE_ROWS, THREE_STEPS, RegisteredMacros(fake.channel), 2, ("open_map", "stay"),
            ("press_start", "look_around"), written_at=1,
        )
        return segment

    segment = asyncio.run(go())

    assert sorted(segment) == [2, 3]
    names, probe, actions = segment[2]
    assert names == ("stay",) and actions == ("look_around",)
    assert probe is not None, "done() 는 step 1 의 것이라 step 2 는 done_2() 로 끝난다"


def test_a_segment_written_at_this_step_ends_it_on_done() -> None:
    async def go():
        fake = _menu()
        return await read_segment(
            THREE_ROWS, THREE_STEPS, RegisteredMacros(fake.channel), 1, ("open_map", "stay"),
            ("press_start", "look_around"), written_at=1,
        )

    segment = asyncio.run(go())

    assert sorted(segment) == [1, 2, 3] and segment[1][1] is None


# --- ```changes 가 받는 이름 -----------------------------------------------------------------------------

DRAFT = PlanDraft(
    1, MACROS + '\n\ndef ready_press_start() -> bool:\n    return scene() == "Menu"\n', (),
    ("press_start", "look_around"),
)


@pytest.mark.parametrize("name", ["done", "ready_press_start", "press_start"])
def test_changes_may_name_any_def_of_the_plan(name: str) -> None:
    changes = read_changes(f'{{"base": 1, "replace": {{"{name}": "it reads the wrong screen"}}}}', DRAFT)

    assert changes.replace == {name: "it reads the wrong screen"}


def test_changes_naming_neither_an_action_nor_a_def_are_refused_with_both_lists() -> None:
    with pytest.raises(MacroRejection) as refused:
        read_changes('{"base": 1, "replace": {"done_9": "x"}}', DRAFT)

    assert "neither an action nor a def" in refused.value.reason
    assert "press_start" in refused.value.reason and "ready_press_start" in refused.value.reason


def test_a_def_named_in_remove_is_dropped_and_one_named_in_replace_takes_the_new_text() -> None:
    changes = read_changes('{"base": 1, "replace": {"done": "x"}, "remove": {"ready_press_start": "y"}}', DRAFT)

    source, _calls = merge(DRAFT, changes, 'def done() -> bool:\n    return scene() == "Menu"\n', [])

    assert "def ready_press_start" not in source
    assert 'return scene() == "Menu"' in source.split("def done")[1]
    assert "def press_start" in source and "def look_around" in source


# --- 시작 검사는 그 step 의 action 만 센다 --------------------------------------------------------------

LOOK_THEN_START = (
    MACROS.replace('def done() -> bool:\n    return scene() == "Map"', 'def done() -> bool:\n    return scene() == "Menu"')
    + '\n\ndef done_2() -> bool:\n    return scene() == "Map"\n'
    + '\n\ndef goal_look() -> bool:\n    return scene() == "Menu"\n'
    + '\n\ndef goal_start() -> bool:\n    return scene() == "Map"\n'
)
LOOK_THEN_START_ANSWER = (
    "```goals\nlook: Look at the menu.\nstart: Press Start.\n```\n"
    "```segment\n1: look |\n2: start | press_start\n```\n"
    f"```python\n{LOOK_THEN_START}```\n"
)


def test_a_looking_first_step_is_not_refused_for_the_next_step_s_action() -> None:
    """로컬 try 31·34·35 step 1: 제목 화면이 떠 있는 것이 step 1 의 결과인데, step 2 의 '시작하기' 때문에 거절됐다."""
    fake = _menu()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        return LOOK_THEN_START_ANSWER

    scenario = QaScenario(
        title="Look then start",
        steps=[
            QaStep(action="Look at the menu.", case_id=5, case=QaCaseRef(id=5, expected="The menu shows Start.")),
            QaStep(action="Press Start.", case_id=6, case=QaCaseRef(id=6, expected="The map opens.")),
        ],
    )

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",)), scenario)

    assert failure is None and state.finished
    assert _logs(fake, "step_macros_refused") == []
    assert len(calls) == 1


# --- 다시 쓴 답이 보낸 묶음 ------------------------------------------------------------------------------

from app.agents.planner_executer.planner import plan_segment  # noqa: E402

KEPT = PlanDraft(1, "", (), (), segment_text="16: a | x\n17: b | y\n18: c | z\n", segment_step=16)


def test_a_rewrite_that_sends_the_whole_segment_again_reads_it_from_the_step_that_first_wrote_it() -> None:
    """로컬 try 38 step 18: 다시 쓰는 요청에 보인 묶음 16~18 을 그대로 다시 보냈고, 지금 step 부터가 아니라고 거절됐다."""
    assert plan_segment("replan", KEPT, KEPT.segment_text, 18) == (KEPT.segment_text, 16, False)


def test_a_rewrite_that_sends_a_segment_from_this_step_writes_it_here() -> None:
    assert plan_segment("replan", KEPT, "18: c | z\n", 18) == ("18: c | z\n", 18, False)


def test_a_rewrite_that_sends_no_segment_keeps_the_plan_s() -> None:
    assert plan_segment("replan", KEPT, None, 17) == (KEPT.segment_text, 16, True)


def test_a_first_plan_reads_its_segment_from_this_step_and_a_travel_takes_none() -> None:
    assert plan_segment("step", None, "3: a | x\n4: b | y\n", 3) == ("3: a | x\n4: b | y\n", 3, False)
    assert plan_segment("travel", None, "3: a | x\n", 3) == (None, None, False)


def test_a_resent_whole_segment_drops_the_rows_already_played() -> None:
    async def go():
        fake = _menu()
        return await read_segment(
            THREE_ROWS, THREE_STEPS, RegisteredMacros(fake.channel), 3, ("open_map", "stay"),
            ("press_start", "look_around"), written_at=1,
        )

    segment = asyncio.run(go())

    assert sorted(segment) == [3] and segment[3][1] is not None
