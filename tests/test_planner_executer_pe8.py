"""pe8: Jev 질문의 막힌 행동, parser 의 bool·`None` 비교, 다음 step 과 겹쳐 도는 판정.

런 418 의 세 문제에서 왔다. step 8 에서 카드를 놓는 행동이 입력 잠금으로 빠지자 Jev 가 대사 넘기기를 고르지 않았고,
planner 답 18개 중 5개가 `pred() == False`·`== None` 으로 거절됐고, 실패한 step 마다 판정 두 호출이 다음 계획을 막았다.
"""

import asyncio

import pytest

from app.agents.planner_executer.executer import blocked_actions_line
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.macro.errors import COMPARISON_REJECTED, MacroRejection
from app.agents.qa.macro.parser import macro_definition_from_source
from app.qa.jev import JevClient
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _config, _menu, _play, _turn_logs, _verdicts
from tests.planner_executer_fakes import FakeGame
from tests.test_qa_macro_runner import FakeHost, LateSelector, card, drive, scene_with

# --- parser: `pred() == False` 와 `== None` -------------------------------------------------------

NEGATED = '''def panel_closed() -> bool:
    return static("View.Current") == None

def m(card: object) -> None:
    if panel_closed() == False:
        press_key("A", 0.1)
    if panel_closed() != False:
        press_key("B", 0.1)
    if static("View.Current") != None:
        press_key("C", 0.1)
'''


def _keys(value) -> list[str]:
    host = FakeHost(
        scene_with(
            card("Root[0]/Go[1]", instance_id=5),
            statics=[{"declaring": "Game.View", "member": "Current", "value": value}],
        )
    )
    result = drive(host, NEGATED, "m", {"card": LateSelector(selector="Root[0]/Go[1]")})
    assert result.failure is None
    return [action.params[0] for actions, _summary, _step in host.sent for action in actions]


def test_a_predicate_compared_with_a_bool_literal_is_read_as_the_predicate_or_its_negation() -> None:
    assert _keys(None) == ["B"]
    assert _keys({"path": "Root/Panel", "world": {"x": 1, "y": 2}}) == ["A", "C"]


def test_a_predicate_against_anything_but_a_bool_literal_and_not_stay_refused() -> None:
    for condition in ("panel_closed() == 3", "not panel_closed()", "panel_closed() > False"):
        source = (
            "def panel_closed() -> bool:\n    return True\n\n"
            f"def m() -> None:\n    if {condition}:\n        press_key(\"A\", 0.1)\n"
        )
        with pytest.raises(MacroRejection):
            macro_definition_from_source("m", source)


def test_none_is_taken_only_beside_equality() -> None:
    for written in ('static("View.Current") > None', 'press_key(None, 0.1)'):
        source = f"def m() -> None:\n    if {written}:\n        press_key(\"A\", 0.1)\n" if ">" in written else (
            f"def m() -> None:\n    {written}\n"
        )
        with pytest.raises(MacroRejection):
            macro_definition_from_source("m", source)


def test_a_null_that_arrives_where_a_number_is_compared_is_still_refused() -> None:
    """`None` 을 적은 비교만 null 을 묻는다. 숫자와 비교한 자리에 null 이 오면 예전처럼 거절이다."""
    host = FakeHost(scene_with(statics=[{"declaring": "Game.View", "member": "Count", "value": None}]))
    source = 'def m() -> None:\n    if static("View.Count") > 2:\n        press_key("A", 0.1)\n'

    result = drive(host, source, "m", {})

    assert result.failure is not None and result.failure.code == COMPARISON_REJECTED


# --- executer: 막힌 행동을 Jev 에게 말한다 --------------------------------------------------------

BLOCKED = MACROS.replace(
    "def done() -> bool:",
    'def ready_press_start() -> bool:\n    return scene() == "Nowhere"\n\n\ndef done() -> bool:',
)


def test_the_pick_question_names_the_actions_left_out_by_their_ready_condition() -> None:
    fake = _menu()

    _play(fake, FakeJev(picks=("look_around",), goal=(0.1,)), source=BLOCKED, max_turns_per_step=2)

    first = next(turn for turn in _turn_logs(fake) if "question" in turn)
    assert first["options"][0] == "look_around"
    assert "Not available right now (their ready condition is false)" in first["question"]
    assert "move on" not in first["question"]
    assert "press_start (Press Start on the title screen)" in first["question"]


def test_the_line_is_empty_when_nothing_is_blocked_or_a_blocked_action_still_has_a_target() -> None:
    fake = _menu()

    async def macros():
        fake.push_reading()
        return await parse_step(MACROS, RegisteredMacros(fake.channel))

    parsed = asyncio.run(macros())
    assert blocked_actions_line([], {"look_around": "x"}, parsed) == ""
    # 같은 행동의 다른 후보가 선택지에 남았으면 막힌 것이 아니다.
    assert blocked_actions_line(["press_start @ Root/A (false)"], {"press_start @ Root/B": "x"}, parsed) == ""
    assert blocked_actions_line(["press_start"], {}, parsed) == ""
    assert "press_start" in blocked_actions_line(["press_start"], {"look_around": "x"}, parsed)


# --- runner: 판정이 다음 step 과 겹쳐 돈다 --------------------------------------------------------


# 맵에 머무는 둘째 step 의 계획: 보기만 한다. 첫 step 이 맵을 열면 `done()` 이 바로 참이다. 동작을 가진 계획(`MACROS`)이면
# 받은 화면에서 이미 참이라 계획 검사가 거절한다(pe11).
LOOK = 'def done() -> bool:\n    return scene() == "Map"\n'


def _two_steps() -> QaScenario:
    return QaScenario(
        title="Start twice",
        steps=[
            QaStep(action="Press Start.", case_id=5, case=QaCaseRef(id=5, expected="The map opens.")),
            QaStep(action="Stay on the map.", case_id=6, case=QaCaseRef(id=6, expected="The map stays.")),
        ],
    )


def test_the_next_step_is_planned_while_the_last_one_is_judged_and_verdicts_keep_their_order() -> None:
    fake = _menu()
    planned_second = asyncio.Event()

    class Planning(Asked):
        async def __call__(self, system: str, user: str) -> str:
            answer = await super().__call__(system, user)
            if len(self.calls) == 2:
                planned_second.set()
            return answer

    judged: list[int] = []

    async def judge(system: str, user: str):
        if not judged:
            # 첫 판정은 둘째 step 의 계획이 시작돼야 끝난다. 겹쳐 돌지 않으면 여기서 시간이 다 된다.
            await asyncio.wait_for(planned_second.wait(), 5)
        judged.append(len(judged) + 1)
        return {"passed": True, "reason": f"step {len(judged)} ok", "state_detail": "", "insight": ""}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=Planning(MACROS, LOOK), judge=judge, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _two_steps())

    state, failure = asyncio.run(go())

    assert failure is None and state.finished
    assert [verdict["step"] for verdict in _verdicts(fake)] == [1, 2]
    assert [verdict["message"] for verdict in _verdicts(fake)] == ["step 1 ok", "step 2 ok"]
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["summary"]["steps"]["passed"] == 2


def test_an_error_while_judging_still_ends_the_run(monkeypatch) -> None:
    """판정이 뒤에서 돌아도 그 안의 예외는 묻히지 않는다. 다음 step 을 시작하기 전에, 늦어도 루프 끝에서 런을 끝낸다."""
    from app.agents.planner_executer import runner as runner_module

    def broken(*args, **kwargs):
        raise RuntimeError("judging broke")

    monkeypatch.setattr(runner_module, "earlier_verdicts", broken)
    fake = _menu()
    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            config, ask=Asked(MACROS, LOOK), judge=lambda *a: None, jev=JevClient(post=FakeJev())
        )
        return await runner.run_with_deadline(fake.channel, _two_steps())

    state, failure = asyncio.run(go())

    assert failure is not None and "judging broke" in str(failure)
    assert not state.finished and _verdicts(fake) == []


# --- pe9: 선택지에 지금의 사실을 붙이고, 기다림은 noul 로 묻는다 -------------------------------------

from app.agents.planner_executer.executer import WAIT_AT, annotate_options  # noqa: E402


def test_options_carry_the_target_s_words_the_streak_and_the_attempts_without_effect() -> None:
    turns = [
        {"picked": "end_turn", "outcome": "passed", "sent": [{"ok": True}]},
        {"picked": "none_fits"},
        {"picked": "end_turn", "outcome": "passed", "sent": [{"ok": True}]},
        {"picked": "place @ Hand/Card[1]", "outcome": "passed", "sent": []},
        {"picked": "end_turn", "outcome": "passed", "sent": [{"ok": True}]},
        {"picked": "end_turn", "outcome": "passed", "sent": [{"ok": True}]},
    ]
    shown = {"Hand/Card[1]": ["Shoot", "Spell"]}.get

    described = annotate_options(
        {"end_turn": "End the turn.", "place @ Hand/Card[1]": "Place a card.", "place @ Hand/Card[2]": "Place a card."},
        turns,
        lambda target: shown(target) or [],
    )

    assert described["end_turn"] == "End the turn.\n\nPicked in each of the last 2 turns."
    assert described["place @ Hand/Card[1]"] == (
        'Place a card.\n\nIt shows: "Shoot", "Spell". Tried 1 time in this step without a visible effect.'
    )
    assert described["place @ Hand/Card[2]"] == "Place a card."


def test_a_high_wait_answer_waits_like_none_fits_and_none_fits_is_no_longer_an_option() -> None:
    fake = _menu()
    jev = FakeJev(picks=("none_fits",), goal=(0.1,))

    play = _play(fake, jev, no_action_grace_seconds=0.05)

    first = jev.asked("choice")[0]
    assert "none_fits" not in first["questions"]["answer"]["criteria"]
    assert first["questions"]["wait"]["type"] == "noul"
    waited = [turn for turn in _turn_logs(fake) if turn.get("wait") is not None]
    assert waited and all(turn["wait"] >= WAIT_AT and turn["picked"] == "none_fits" for turn in waited)
    assert fake.actions == [] and play.stopped.startswith("no action fit")


# --- pe9: planner 의 하위 목표(```goals, prompt v6) ------------------------------------------------

from app.agents.planner_executer.macros import read_goals  # noqa: E402

GOALS_MACROS = MACROS.replace(
    "def done() -> bool:",
    'def goal_open_map() -> bool:\n    return scene() == "Map"\n\n\ndef goal_stay() -> bool:\n    return scene() == "Map"\n\n\ndef done() -> bool:',
)


def _read(text: str, source: str = GOALS_MACROS):
    fake = _menu()

    async def go():
        fake.push_reading()
        return await read_goals(text, source, RegisteredMacros(fake.channel))

    return asyncio.run(go())


def test_goals_are_read_in_order_with_a_predicate_each() -> None:
    goals, probes = _read("open_map: Press Start to open the map.\nstay: Stay on the map.\n")

    assert goals == (("open_map", "Press Start to open the map."), ("stay", "Stay on the map."))
    assert set(probes) == {"open_map", "stay"}
    assert _read("") == ((), {})


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Open the map", "`<name>: <goal>`"),
        ("open_map: a\nopen_map: b", "listed twice"),
        ("missing: Do it.", "needs `def goal_missing() -> bool:`"),
    ],
)
def test_a_goals_block_that_cannot_be_read_is_refused(text: str, said: str) -> None:
    with pytest.raises(MacroRejection) as refused:
        _read(text)
    assert said in refused.value.reason


def test_jev_reads_the_current_goal_instead_of_the_step_and_moves_on_when_it_is_reached() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start", "look_around"), goal=(0.1,))

    async def go():
        fake.push_reading()
        # 완료 조건이 끝내 참이 되지 않아야 둘째 하위 목표까지 간다. 참이 되면 코드가 바로 멈춘다(pe11).
        source = GOALS_MACROS.replace('def done() -> bool:\n    return scene() == "Map"', 'def done() -> bool:\n    return scene() == "Never"')
        assert source != GOALS_MACROS
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        goals, probes = await read_goals(
            "open_map: Press Start to open the map.\nstay: Stay on the map.\n", source, RegisteredMacros(fake.channel)
        )
        from dataclasses import replace

        from app.agents.planner_executer.arch import PlannerExecuterArchSpec
        from app.agents.planner_executer.executer import Executer

        spec = PlannerExecuterArchSpec(
            no_action_grace_seconds=0.1, state_source="pulse_memory", max_turns_per_step=3
        )
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(
            1, "Press Start and look at the map.", replace(macros, goals=goals, goal_probes=probes)
        )

    asyncio.run(go())

    questions = [body["questions"]["answer"]["instructions"] for body in jev.asked("choice")]
    assert questions[0] == "Which of these should the player do now to reach this goal? The goal: Press Start to open the map."
    assert "The step:" not in questions[0]
    assert questions[1].endswith("The goal: Stay on the map.")
    picks = [turn for turn in _turn_logs(fake) if turn.get("goal")]
    assert [turn["goal"] for turn in picks[:2]] == ["open_map", "stay"]


def test_a_developer_macro_keeps_its_own_ready_condition_over_the_planner_s() -> None:
    from app.agents.planner_executer.macros import _with_developer_readies

    planner_ready, developer_ready = object(), object()

    class Action:
        ready = developer_ready

    merged = _with_developer_readies({"developer:tap": planner_ready, "mine": planner_ready}, {"developer:tap": Action()})

    assert merged["developer:tap"] is developer_ready and merged["mine"] is planner_ready


# --- pe9: step 경계 ---------------------------------------------------------------------------------

from app.agents.planner_executer.executer import no_effect_shown  # noqa: E402
from app.agents.planner_executer.macros import read_segment, step_of_segment  # noqa: E402


def test_play_stops_the_moment_done_turns_true_without_asking_jev() -> None:
    fake = _menu()  # `done()` 은 처음에 거짓이고, Start 를 누르면 참이 된다
    jev = FakeJev(picks=("press_start",), goal=(0.1,))

    play = _play(fake, jev)

    assert play.stopped == "done" and [turn.get("picked") for turn in play.turns] == ["press_start", None]
    assert jev.asked("noul") == []


def test_a_done_true_from_the_start_stops_an_acting_step_without_input_unless_the_runner_checked_it() -> None:
    """처음부터 참인 완료 조건은 이 step 의 결과가 아닐 수 있다(try 14 step 4). pe11 부터 Jev 에게 묻지 않는다. 계획 검사를 거치지
    않았으면 입력 없이 `done_at_start` 로 멈춘다. runner 가 계획을 받을 때 거짓으로 읽었으면 그 뒤에 참이 된 것이라 `done` 이다."""
    unchecked = FakeGame("Map", {"Root/Stage": {"text": "Stage 0"}})
    jev = FakeJev(picks=("look_around",))

    play = _play(unchecked, jev)

    assert play.stopped == "done_at_start" and unchecked.actions == [] and jev.bodies == []

    checked = FakeGame("Map", {"Root/Stage": {"text": "Stage 0"}})

    async def go():
        checked.push_reading()
        from app.agents.planner_executer.arch import PlannerExecuterArchSpec
        from app.agents.planner_executer.executer import Executer

        macros = await parse_step(MACROS, RegisteredMacros(checked.channel))
        spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.1, state_source="pulse_memory")
        return await Executer(checked.channel, spec, JevClient(post=jev)).play(1, "Press Start.", macros, checked=True)

    assert asyncio.run(go()).stopped == "done" and checked.actions == []


def test_a_step_that_only_looks_takes_a_done_true_from_the_start() -> None:
    fake = FakeGame("Map", {"Root/Stage": {"text": "Stage 0"}})
    jev = FakeJev(picks=("look_around",), goal=(0.1,))
    only_done = 'def done() -> bool:\n    return scene() == "Map"\n'

    play = _play(fake, jev, source=only_done)

    assert play.stopped == "done" and jev.asked("noul") == []


def test_an_effect_that_was_already_true_counts_as_no_effect() -> None:
    assert no_effect_shown({"effect": "already"}) and no_effect_shown({"result": "already_true"})
    assert no_effect_shown({"effect": "unmet"}) and not no_effect_shown({"effect": "met"})
    described = annotate_options(
        {"enter": "Enter the stage."},
        [{"picked": "enter", "outcome": "passed", "sent": [{"ok": True}], "effect": "already"}],
        lambda target: [],
    )
    assert described["enter"].endswith("Tried 1 time in this step without a visible effect.")


SEGMENT_SOURCE = GOALS_MACROS.replace(
    "def done() -> bool:", 'def done_2() -> bool:\n    return scene() == "Map"\n\n\ndef done() -> bool:'
)


def _segment(text: str, source: str = SEGMENT_SOURCE):
    fake = _menu()

    async def go():
        fake.push_reading()
        return await read_segment(
            text, source, RegisteredMacros(fake.channel), 1, ("open_map", "stay"), ("press_start", "look_around")
        )

    return asyncio.run(go())


def test_a_segment_names_the_following_steps_with_their_goals_and_done() -> None:
    segment = _segment("1: open_map | press_start\n2: stay |\n")

    assert segment[1] == (("open_map",), None, ("press_start",))
    assert segment[2][0] == ("stay",) and segment[2][1] is not None and segment[2][2] == ()
    assert _segment("") == {}


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("2: stay |", "starting with the step you are writing (1)"),
        ("1: open_map |\n3: stay |", "lists [1, 3]"),
        ("1: open_map |\n2: nowhere |", "not in the ```goals block"),
        ("1: open_map |\n2: stay |\n3: stay |", "`def done_3() -> bool:`"),
        ("1: open_map\n2: stay", "a step that only looks lists no action"),
        ("1: open_map | fly\n2: stay |", "`fly`, which is not an action of this plan"),
    ],
)
def test_a_segment_that_cannot_be_read_is_refused(text: str, said: str) -> None:
    with pytest.raises(MacroRejection) as refused:
        _segment(text)
    assert said in refused.value.reason


SEGMENT_ANSWER = (
    "```summary\nPress Start, then stay on the map.\n```\n"
    "```goals\nopen_map: Press Start to open the map.\nstay: Stay on the map.\n```\n"
    "```segment\n1: open_map | press_start\n2: stay | look_around\n```\n"
    f"```python\n{SEGMENT_SOURCE}```\n"
)


def test_the_next_step_of_a_segment_is_played_without_asking_the_planner_again() -> None:
    fake = _menu()
    calls: list[str] = []

    async def ask(system: str, user: str) -> str:
        calls.append(user)
        return SEGMENT_ANSWER

    async def judge(system: str, user: str):
        return {"passed": True, "reason": "ok", "state_detail": "", "insight": ""}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=ask, judge=judge, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _two_steps())

    state, failure = asyncio.run(go())

    assert failure is None and state.finished
    assert len(calls) == 1
    assert "Steps after this one" in calls[0] and '"do": "Stay on the map."' in calls[0]
    assert [verdict["step"] for verdict in _verdicts(fake)] == [1, 2]
    reused = [f for f in fake.frames("LOG") if f["payload"].get("kind") == "segment_step"]
    assert len(reused) == 1 and reused[0]["payload"]["step"] == 2


def test_the_steps_of_a_segment_are_judged_after_the_segment_on_its_whole_record() -> None:
    fake = _menu()
    asked: list[str] = []
    judged: list[str] = []

    async def ask(system: str, user: str) -> str:
        asked.append(user)
        return SEGMENT_ANSWER

    async def judge(system: str, user: str):
        # 묶음의 첫 step 판정은 둘째 step 을 다 돈 뒤에 온다.
        judged.append(user)
        assert any("turn" in f["payload"].get("kind", "") and f["payload"].get("step") == 2 for f in fake.frames("LOG"))
        return {"passed": True, "reason": "ok", "state_detail": "", "insight": ""}

    config = _config(no_action_grace_seconds=0.2, analysis="off", coverage_check="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=ask, judge=judge, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _two_steps())

    state, failure = asyncio.run(go())

    assert failure is None and state.finished
    assert [verdict["step"] for verdict in _verdicts(fake)] == [1, 2]
    assert len(judged) == 2
    assert all("Steps 1, 2 were played under one plan" in user for user in judged)
    assert "Judge step 1 on the whole record" in judged[0] and "Judge step 2 on the whole record" in judged[1]
    assert "whichever step's turns it falls in, the state the step starts from held" in judged[0]
    assert "A change that a different move caused does not count" in judged[0]
    assert "step 1 · turn" in judged[0] and "step 2 · turn" in judged[0], "첫 step 판정도 둘째 step 의 turn 을 읽는다"
    together = [f for f in fake.frames("LOG") if f["payload"].get("kind") == "segment_judgement"]
    assert len(together) == 1 and together[0]["payload"]["decision"]["steps"] == [1, 2]


def test_step_of_segment_swaps_in_the_step_s_done_and_goals() -> None:
    fake = _menu()

    async def go():
        fake.push_reading()
        registered = RegisteredMacros(fake.channel)
        macros = await parse_step(SEGMENT_SOURCE, registered)
        goals, probes = await read_goals("open_map: a\nstay: b\n", SEGMENT_SOURCE, registered)
        segment = await read_segment(
            "1: open_map | press_start\n2: stay | look_around\n", SEGMENT_SOURCE, registered, 1, ("open_map", "stay"),
            ("press_start", "look_around"),
        )
        from dataclasses import replace

        return replace(macros, goals=goals, goal_probes=probes, segment=segment)

    macros = asyncio.run(go())
    first, second = step_of_segment(macros, 1), step_of_segment(macros, 2)

    assert first.probe is macros.probe and first.goals == (("open_map", "a"),)
    assert second.probe is macros.segment[2][1] and second.goals == (("stay", "b"),)
    # 묶음의 action 은 그 step 에 적힌 것만 열린다.
    assert list(first.actions) == ["press_start"] and list(second.actions) == ["look_around"]


# --- pe9: legacy 모드도 developer macro 의 등록 계약을 쓴다 ---------------------------------------------

from app.agents.planner_executer.developer_macros import (  # noqa: E402
    DeveloperMacro,
    bind_developer_calls_with_contracts,
    load_developer_macros,
)

STRIKE = '''def strike(enemy: object) -> None:
    """Strike one living enemy so it loses health."""
    click(enemy)


def ready_strike(enemy: object) -> bool:
    return member(enemy, "Game.Enemy::hp") > 0


def effect_strike(enemy: object) -> bool:
    return member(enemy, "Enemy.hit") == True
'''
WAIT = '''def wait_a_bit() -> None:
    """Press Space once."""
    press_key("Space", 0.05)
'''
STRIKE_CONTRACT = {"params": {"enemy": {"holds": "Game.Enemy::hp"}}, "effect": {"mode": "transition", "within": 0.5}}


def test_a_registered_contract_is_used_in_legacy_mode_and_the_planner_s_ready_for_it_is_ignored() -> None:
    fake = _menu()

    async def go():
        fake.push_reading()
        loaded = await load_developer_macros((
            DeveloperMacro("strike", STRIKE, "1.0", None, False, STRIKE_CONTRACT),
            DeveloperMacro("wait_a_bit", WAIT, "1.0", None, False, None),
        ))
        actions, contracts, binder, pairs = await bind_developer_calls_with_contracts(
            ['strike(pick("Enemy"))', "wait_a_bit"], loaded
        )
        source = (
            'def ready_strike() -> bool:\n    return scene() == "Battle"\n\n\n'
            'def done() -> bool:\n    return scene() == "Done"\n'
        )
        macros = await parse_step(source, RegisteredMacros(fake.channel), actions, None, contract_keys=tuple(contracts))
        return actions, contracts, pairs, macros

    actions, contracts, pairs, macros = asyncio.run(go())

    assert list(contracts) == ["developer:strike"] and list(actions) == ["developer:wait_a_bit"]
    assert pairs == (("developer:strike", 'strike(pick("Enemy"))'), ("developer:wait_a_bit", "wait_a_bit"))
    assert "developer:strike" not in macros.readies


def test_a_legacy_step_with_a_contracted_developer_macro_runs_end_to_end() -> None:
    """try 15 는 계약 행동의 설명이 빠져 `KeyError: 'developer:start_new_game'` 로 시작하자마자 멈췄다. planner → executer →
    evaluator 를 한 번 끝까지 돈다."""
    from tests.test_planner_executer_developer_macros import ONLY_DONE, Answered, _answer, _run

    tap = '''def tap_start() -> None:
    """Press Start on the title screen."""
    click(selector("Root/Start"))


def effect_tap_start() -> bool:
    return scene() == "Map"
'''
    contract = {"params": {}, "effect": {"mode": "transition", "within": 1.0}}
    fake = _menu()

    state, failure = _run(
        fake,
        Answered(_answer(ONLY_DONE, "tap_start")),
        (DeveloperMacro("tap_start", tap, "1.2.0", None, False, contract),),
        FakeJev(picks=("developer:tap_start",)),
        analysis="off",
    )

    assert failure is None and state.finished and state.step_results[0].passed
    assert fake.scene == "Map"
    plan = next(f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros")
    assert plan["decision"]["descriptions"]["developer:tap_start"] == "Press Start on the title screen."


# --- pe9: planner 진행 표시 ------------------------------------------------------------------------


def test_the_planner_streams_its_answer_and_notes_its_progress(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.agents.planner_executer import planner as planner_module
    from app.agents.planner_executer.planner import Planner

    monkeypatch.setattr(planner_module, "PROGRESS_SECONDS", 0.01)

    class Streaming:
        async def astream(self, messages):
            for piece in ("```python\n", "def done() -> bool:\n", "    return True\n```"):
                await asyncio.sleep(0.03)
                yield SimpleNamespace(content=piece)

    class Broken:
        async def astream(self, messages):
            raise RuntimeError("no streaming here")
            yield  # pragma: no cover

        async def ainvoke(self, messages):
            return SimpleNamespace(content="whole answer")

    fake = _menu()
    config = _config()

    async def go(model):
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel))
        planner._model = model
        planner._progress = ("Step 1", 1)
        return await planner._ask_model("system", "user")

    assert asyncio.run(go(Streaming())).endswith("return True\n```")
    progress = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "planner_progress"]
    assert progress and progress[0]["step"] == 1
    assert max(entry["decision"]["chars"] for entry in progress) > 0
    assert asyncio.run(go(Broken())) == "whole answer"


def test_the_planner_shows_the_reasoning_summary_before_it_writes(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.agents.planner_executer import planner as planner_module
    from app.agents.planner_executer.planner import Planner

    monkeypatch.setattr(planner_module, "PROGRESS_SECONDS", 0.01)
    asked: list[dict] = []

    class Reasoning:
        reasoning = {"effort": "high"}

        def __init__(self, refuse=False):
            self.refuse = refuse

        def bind(self, **kwargs):
            asked.append(kwargs["reasoning"])
            outer = self

            class Bound:
                async def astream(self, messages):
                    if outer.refuse:
                        raise RuntimeError("organization must be verified to generate reasoning summaries")
                    await asyncio.sleep(0.03)
                    yield SimpleNamespace(content=[{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Checking which"}]}])
                    # langchain-openai 의 v0 형식: `additional_kwargs` 에 `type` 없이 온다.
                    await asyncio.sleep(0.03)
                    yield SimpleNamespace(
                        content=[], additional_kwargs={"reasoning": {"summary": [{"type": "summary_text", "text": " slot is empty"}]}}
                    )
                    await asyncio.sleep(0.03)
                    yield SimpleNamespace(content=[{"type": "text", "text": "answer"}])

            return Bound()

        async def ainvoke(self, messages):
            return SimpleNamespace(content="whole answer")

    fake = _menu()
    config = _config()

    async def go(model):
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel))
        planner._model = model
        planner._progress = ("Step 1", 1)
        return planner, await planner._ask_model("system", "user")

    _, answer = asyncio.run(go(Reasoning()))
    assert answer == "answer"
    assert asked == [{"effort": "high", "summary": "auto"}]
    thinking = [f["payload"]["decision"]["thinking"] for f in fake.frames("LOG") if f["payload"].get("kind") == "planner_progress"]
    assert "Checking which slot is empty" in thinking

    planner, answer = asyncio.run(go(Reasoning(refuse=True)))
    assert answer == "whole answer"
    assert planner._streaming_model()[1] is False


# --- pe9: 다음 step 계획 미리 쓰기 -----------------------------------------------------------------------


def _run_ahead(fake, asked, monkeypatch, scenes=None):
    from app.agents.planner_executer import runner as runner_module

    if scenes is not None:
        # 씬 변화 추적은 끄고, 계획을 쓸 때와 쓰려 할 때의 씬만 바꿔 본다.
        monkeypatch.setattr(runner_module, "SCENE_POLL_SECONDS", 3600)
        # 다 쓰면 마지막 씬이 이어진다.
        seen = iter([*scenes, *[scenes[-1]] * 50])
        monkeypatch.setattr(runner_module, "current_scene", lambda _channel: next(seen))

    async def judge(system: str, user: str):
        return {"passed": True, "reason": "ok", "state_detail": "", "insight": ""}

    config = _config(no_action_grace_seconds=0.2, analysis="off", plan_ahead="on")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _two_steps())

    return asyncio.run(go())


def test_the_next_step_is_written_ahead_while_the_step_before_it_plays_and_used_on_the_same_scene(monkeypatch) -> None:
    fake = _menu()
    asked = Asked(MACROS, LOOK)

    state, failure = _run_ahead(fake, asked, monkeypatch, scenes=["Menu", "Menu"])

    assert failure is None and state.finished
    assert len(asked.calls) == 2, "둘째 step 은 다시 묻지 않는다"
    assert "This plan is written ahead, while step 1 is still being played" in asked.calls[1][1]
    plans = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros"]
    assert [plan["step"] for plan in plans] == [1, 2]
    assert plans[1]["decision"]["written_ahead"] is True
    assert [verdict["step"] for verdict in _verdicts(fake)] == [1, 2]


def test_a_plan_written_ahead_on_another_scene_is_dropped_and_the_step_is_written_again(monkeypatch) -> None:
    fake = _menu()
    asked = Asked(MACROS, LOOK, LOOK)

    # 쓰기 시작할 때 Menu, step 1 이 끝날 때 Battle → 끝날 때 다시 쓰기 시작(Battle), 쓰려 할 때 Menu → 기다리지 않고 버림.
    state, failure = _run_ahead(fake, asked, monkeypatch, scenes=["Menu", "Battle", "Battle", "Menu", "Menu"])

    assert failure is None and state.finished
    assert len(asked.calls) == 3
    dropped = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "ahead_plan_dropped"]
    assert len(dropped) == 1 and "written on `Battle`, and the screen is now on `Menu`" in dropped[0]["message"]
    plans = [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros"]
    assert [plan["step"] for plan in plans] == [1, 2] and not plans[1]["decision"].get("written_ahead")
