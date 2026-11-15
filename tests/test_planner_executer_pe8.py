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

    config = _config(no_action_grace_seconds=0.2, analysis="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=Planning(MACROS, MACROS), judge=judge, jev=JevClient(post=FakeJev()))
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
    config = _config(no_action_grace_seconds=0.2, analysis="off")

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            config, ask=Asked(MACROS, MACROS), judge=lambda *a: None, jev=JevClient(post=FakeJev())
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


def test_the_old_way_is_kept_behind_the_knob() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    _play(fake, jev, pick_wait="option")

    first = jev.asked("choice")[0]
    assert "none_fits" in first["questions"]["answer"]["criteria"] and "wait" not in first["questions"]


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
        macros = await parse_step(GOALS_MACROS, RegisteredMacros(fake.channel))
        goals, probes = await read_goals(
            "open_map: Press Start to open the map.\nstay: Stay on the map.\n", GOALS_MACROS, RegisteredMacros(fake.channel)
        )
        from dataclasses import replace

        from app.agents.planner_executer.arch import PlannerExecuterArchSpec
        from app.agents.planner_executer.executer import Executer

        spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.1, state_source="pulse_memory", max_turns_per_step=3)
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
