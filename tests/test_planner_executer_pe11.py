"""pe11: Jev 는 준비된 선택지가 둘 이상일 때 고르기만 하고, 흐름은 코드가 정한다.

- 선택지가 하나면 묻지 않는다. 기다림은 준비 조건이 참으로 읽히지 않는 선택지가 있을 때만 묻는다.
- 묶음의 뒤 step `done_k` 가 참이 되면 지금 step 은 거기서 끝나고, 그 사이 step 은 입력 없이 끝난다.
- 계획을 받은 화면에서 끝 조건이 이미 참이면 planner 에게 다시 쓰게 한다.
- ORDERED step 의 씬이 바뀌고 유예 동안 끝 조건이 거짓이면 `off_plan` 으로 멈춘다.
- planner·editor 가 쓴 글의 `ask`·`decide` 는 거절한다.
"""

import asyncio
from dataclasses import replace

import pytest

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import (
    DONE_AT_START,
    LATER_DONE,
    PASSED_IN_EARLIER_STEP,
    RESULT_IN_EARLIER_STEP,
    Executer,
)
from app.agents.planner_executer.macros import RegisteredMacros, parse_step, read_goals, read_segment, step_of_segment
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _config, _menu, _play, _verdicts

# 두 action 모두 준비 조건이 있고, 메뉴에서는 둘 다 참이다.
READY_BOTH = MACROS + (
    '\n\ndef ready_press_start() -> bool:\n    return scene() == "Menu"\n'
    '\n\ndef ready_look_around() -> bool:\n    return scene() == "Menu"\n'
)
READY_ONE = MACROS + '\n\ndef ready_press_start() -> bool:\n    return scene() == "Menu"\n'
ONLY_PRESS = MACROS.split("\n\ndef look_around")[0] + (
    '\n\n\ndef ready_press_start() -> bool:\n    return scene() == "Menu"\n'
    '\n\ndef done() -> bool:\n    return scene() == "Map"\n'
)


# --- 기다림과 고르기 --------------------------------------------------------------------------------


def test_options_whose_ready_conditions_all_hold_are_picked_without_asking_whether_to_wait() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",))

    play = _play(fake, jev, READY_BOTH)

    (pick,) = jev.asked("choice")
    assert "wait" not in pick["questions"]
    assert "wait_asked_because" not in play.turns[0] and play.turns[0]["picked_by"] == "jev"
    assert play.stopped == "done"


def test_one_option_without_a_ready_condition_brings_the_wait_question_back() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",))

    play = _play(fake, jev, READY_ONE)

    (pick,) = jev.asked("choice")
    assert pick["questions"]["wait"]["type"] == "noul"
    assert play.turns[0]["wait_asked_because"] == ["look_around"]


def test_a_single_ready_option_runs_without_any_jev_call() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",))

    play = _play(fake, jev, ONLY_PRESS)

    assert jev.bodies == []
    assert play.turns[0]["picked"] == "press_start" and play.turns[0]["picked_by"] == "only_option"
    assert play.stopped == "done" and fake.scene == "Map"


# --- 묶음: 뒤 step 의 완료 조건 ----------------------------------------------------------------------

# step 1 의 완료 조건은 끝내 참이 되지 않고, Start 를 누르면 step 2 의 완료 조건이 참이 된다(try 25 step 4·5 의 모양).
LATER_SOURCE = (
    MACROS.replace('def done() -> bool:\n    return scene() == "Map"', 'def done() -> bool:\n    return scene() == "Never"')
    + '\n\ndef done_2() -> bool:\n    return scene() == "Map"\n'
    + '\n\ndef goal_open_map() -> bool:\n    return scene() == "Map"\n'
    + '\n\ndef goal_stay() -> bool:\n    return scene() == "Map"\n'
)
GOALS_TEXT = "open_map: Press Start to open the map.\nstay: Stay on the map.\n"
SEGMENT_TEXT = "1: open_map | press_start, look_around\n2: stay | look_around\n"
LATER_ANSWER = (
    "```summary\nPress Start, then stay on the map.\n```\n"
    f"```goals\n{GOALS_TEXT}```\n"
    f"```segment\n{SEGMENT_TEXT}```\n"
    f"```python\n{LATER_SOURCE}```\n"
)


def _segment_macros(fake):
    async def go():
        registered = RegisteredMacros(fake.channel)
        macros = await parse_step(LATER_SOURCE, registered)
        goals, probes = await read_goals(GOALS_TEXT, LATER_SOURCE, registered)
        segment = await read_segment(
            SEGMENT_TEXT, LATER_SOURCE, registered, 1, ("open_map", "stay"), ("press_start", "look_around")
        )
        return replace(macros, goals=goals, goal_probes=probes, segment=segment)

    return go()


def _play_segment_step(fake, jev, checked: bool):
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", max_turns_per_step=4)

    async def go():
        fake.push_reading()
        macros = step_of_segment(await _segment_macros(fake), 1)
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Read the story.", macros, checked=checked)

    return asyncio.run(go())


def test_a_later_step_s_done_becoming_true_ends_the_step_being_played() -> None:
    fake = _menu()

    play = _play_segment_step(fake, FakeJev(picks=("press_start",)), checked=True)

    assert play.stopped == LATER_DONE and play.later_step == 2
    assert play.turns[-1]["later_done"] == [2] and "picked" not in play.turns[-1]
    assert [turn.get("picked") for turn in play.turns] == ["press_start", None]
    assert play.reached_goal()


def test_a_later_done_already_true_counts_only_once_it_was_read_false() -> None:
    """계획 검사를 거치지 않은 play 에서 처음부터 참인 뒤 step 의 완료 조건은 이 play 안에서 생긴 결과가 아니다."""
    fake = _menu()
    fake.scene = "Map"

    play = _play_segment_step(fake, FakeJev(picks=("look_around",)), checked=False)

    assert play.stopped != LATER_DONE and play.later_step is None


def _two_steps() -> QaScenario:
    return QaScenario(
        title="Story then map",
        steps=[
            QaStep(action="Read the story.", case_id=5, case=QaCaseRef(id=5, expected="The story goes on.")),
            QaStep(action="Reach the map.", case_id=6, case=QaCaseRef(id=6, expected="The map opens.")),
        ],
    )


def _run(fake, ask, jev, scenario=None, **knobs):
    async def judge(system: str, user: str):
        return {"passed": True, "reason": "ok", "state_detail": "", "insight": ""}

    config = _config(no_action_grace_seconds=0.2, analysis="off", **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=ask, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, scenario or _two_steps())

    return asyncio.run(go())


def test_the_step_whose_result_came_in_the_earlier_step_ends_without_input_and_the_segment_is_judged_together() -> None:
    fake = _menu()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        return LATER_ANSWER

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",)))

    assert failure is None and state.finished
    assert len(calls) == 1
    stopped = [verdict["played"]["stopped"] for verdict in _verdicts(fake)]
    assert stopped == [LATER_DONE, RESULT_IN_EARLIER_STEP]
    second = [f["payload"]["turn"] for f in fake.frames("LOG") if f["payload"].get("kind") == "turn" and f["payload"].get("step") == 2]
    assert len(second) == 1 and "picked" not in second[0]
    assert [name for name, _params in fake.actions].count("mouse_down") == 1  # Start 를 한 번만 눌렀다
    together = [f for f in fake.frames("LOG") if f["payload"].get("kind") == "segment_judgement"]
    assert len(together) == 1 and together[0]["payload"]["decision"]["steps"] == [1, 2]


def test_steps_between_the_one_played_and_the_later_result_end_without_input() -> None:
    fake = _menu()
    source = LATER_SOURCE.replace("def done_2() -> bool:", 'def done_2() -> bool:\n    return scene() == "Never"\n\n\ndef done_3() -> bool:')
    answer = LATER_ANSWER.replace(LATER_SOURCE, source).replace(
        SEGMENT_TEXT, "1: open_map | press_start, look_around\n2: stay | look_around\n3: stay | look_around\n"
    )
    scenario = QaScenario(
        title="Story, middle, map",
        steps=[*_two_steps().steps[:1], QaStep(action="Keep reading.", case_id=7), _two_steps().steps[1]],
    )

    async def ask(system: str, user: str) -> str:
        return answer

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",)), scenario)

    assert failure is None and state.finished
    stopped = [verdict["played"]["stopped"] for verdict in _verdicts(fake)]
    assert stopped == [LATER_DONE, PASSED_IN_EARLIER_STEP, RESULT_IN_EARLIER_STEP]


# --- 계획 검사 ------------------------------------------------------------------------------------

LOOK = 'def done() -> bool:\n    return scene() == "Map"\n'


def _start_then_stay() -> QaScenario:
    return QaScenario(
        title="Start, then stay",
        steps=[
            QaStep(action="Press Start.", case_id=5, case=QaCaseRef(id=5, expected="The map opens.")),
            QaStep(action="Stay on the map.", case_id=6, case=QaCaseRef(id=6, expected="The map stays.")),
        ],
    )


def test_a_plan_whose_done_already_holds_on_the_screen_it_was_written_on_is_written_again() -> None:
    fake = _menu()
    asked = Asked(MACROS, MACROS, LOOK)

    state, failure = _run(fake, asked, FakeJev(picks=("press_start",)), _start_then_stay())

    assert failure is None and state.finished
    assert len(asked.calls) == 3
    refused = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros_refused"]
    assert len(refused) == 1 and "`done()` already holds on the screen this plan was written on" in refused[0]["message"]
    assert "already holds on the screen" in asked.calls[2][1]
    assert [verdict["played"]["stopped"] for verdict in _verdicts(fake)] == ["done", "done"]


def test_the_last_answer_is_taken_even_when_its_done_still_holds_and_play_sends_no_input() -> None:
    fake = _menu()
    asked = Asked(MACROS, MACROS, MACROS, MACROS)

    state, failure = _run(fake, asked, FakeJev(picks=("press_start",)), _start_then_stay())

    assert failure is None and state.finished
    unchecked = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_start_unchecked"]
    assert len(unchecked) == 1
    assert [verdict["played"]["stopped"] for verdict in _verdicts(fake)] == ["done", DONE_AT_START]


# --- 씬이 바뀜 -----------------------------------------------------------------------------------

NEVER = MACROS.replace('def done() -> bool:\n    return scene() == "Map"', 'def done() -> bool:\n    return scene() == "Never"')


def _play_bound(fake, jev, source, can_replan_scene=False):
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory")

    async def go():
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(
            1, "Press Start.", macros, scene_bound=True, can_replan_scene=can_replan_scene
        )

    return asyncio.run(go())


def test_a_step_whose_scene_changed_waits_without_input_and_stops_off_plan() -> None:
    fake = _menu()

    play = _play_bound(fake, FakeJev(picks=("press_start",)), NEVER)

    assert play.stopped.startswith("off_plan: the scene changed from `Menu` to `Map`")
    assert play.left_scene == "Map" and not play.replan_requested
    after = [turn for turn in play.turns if turn.get("scene_left")]
    assert after and all("picked" not in turn for turn in after)
    assert [name for name, _params in fake.actions].count("mouse_down") == 1  # Start 를 한 번만 눌렀다


def test_off_plan_asks_for_the_step_to_be_written_again_when_allowed() -> None:
    play = _play_bound(_menu(), FakeJev(picks=("press_start",)), NEVER, can_replan_scene=True)

    assert play.replan_requested and play.left_scene == "Map"


def test_done_on_the_new_scene_stops_as_done_before_off_plan() -> None:
    play = _play_bound(_menu(), FakeJev(picks=("press_start",)), MACROS)

    assert play.stopped == "done"


# --- planner 의 글 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '    while ask("Is there another line?"):\n        press_key("Space", 0.05)\n',
        '    match decide("Which one?"):\n        case "tap":\n            tap()\n        case "wave":\n            wave()\n',
    ],
)
def test_a_plan_that_hands_a_judgement_to_the_decision_model_is_refused(body: str) -> None:
    source = 'def read_on() -> None:\n    """Read on."""\n' + body + '\n\ndef done() -> bool:\n    return scene() == "Map"\n'
    fake = _menu()

    async def go():
        fake.push_reading()
        return await parse_step(source, RegisteredMacros(fake.channel))

    with pytest.raises(MacroRejection) as refused:
        asyncio.run(go())
    assert "hands a judgement to the decision model" in refused.value.reason


def test_the_step_and_the_later_step_done_together_end_both_without_breaking_the_segment() -> None:
    """한 입력으로 지금 step 과 뒤 step 의 완료 조건이 함께 참이 되면 지금 step 은 `done`, 뒤 step 은 결과가 앞에서 난 것이다."""
    fake = _menu()
    source = LATER_SOURCE.replace('def done() -> bool:\n    return scene() == "Never"', 'def done() -> bool:\n    return scene() == "Map"')

    async def ask(system: str, user: str) -> str:
        return LATER_ANSWER.replace(LATER_SOURCE, source)

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",)))

    assert failure is None and state.finished
    assert [verdict["played"]["stopped"] for verdict in _verdicts(fake)] == ["done", RESULT_IN_EARLIER_STEP]


def test_a_segment_whose_next_done_already_holds_where_the_step_stopped_is_dropped_and_written_again() -> None:
    """마지막 답까지 거절돼 그대로 받은 계획에서, 뒤 step 의 완료 조건이 앞 step 이 멈춘 곳에서 이미 참이면 묶음을 끊고 새로 쓴다."""
    fake = _menu()
    source = LATER_SOURCE.replace('def done() -> bool:\n    return scene() == "Never"', 'def done() -> bool:\n    return scene() == "Map"').replace(
        'def done_2() -> bool:\n    return scene() == "Map"', 'def done_2() -> bool:\n    return scene() != "Never"'
    )
    asked = Asked(LOOK)
    answers = [LATER_ANSWER.replace(LATER_SOURCE, source)]

    async def ask(system: str, user: str) -> str:
        return answers.pop(0) if answers else await asked(system, user)

    state, failure = _run(fake, ask, FakeJev(picks=("press_start",)), max_rewrites=0)

    assert failure is None and state.finished
    broken = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "segment_broken"]
    assert len(broken) == 1 and broken[0]["step"] == 2
    assert len(asked.calls) == 1, "둘째 step 은 새로 쓴다"
    assert [verdict["played"]["stopped"] for verdict in _verdicts(fake)] == ["done", "done"]
