"""헛도는 action 의 복구를 planner 에게 맡긴다 (개선 계획 묶음 2).

`repair="planner"` 면 executer 가 play 를 멈추고, runner 가 실패 기록과 게임 값의 변화를 실어 planner 에게 step 을 다시
쓰게 한 뒤 이어서 play 한다. evaluator 는 step 의 play 를 전부 이어 붙여 읽는다.
"""

from app.agents.planner_executer.executer import StepPlay, merge_plays
from app.agents.planner_executer.runner import repair_brief
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import (
    MACROS,
    Asked,
    FakeJev,
    _judge,
    _run,
    _verdicts,
)

# Start 를 눌러도 Map 이 늦게 열려 `press_start` 의 기다림이 매번 끝난다.
SLOW = MACROS.replace('wait_until(scene() == "Map", 1, "Start did not open the map.")', 'wait_until(scene() == "Shop", 1, "No shop.")')


class Verbatim(Asked):
    """The planner's model answering whole texts, so an answer can carry a ```changes block."""

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answers.pop(0)


def _slow_menu() -> FakeGame:
    return FakeGame("Menu", {"Root/Start": {"text": "Start"}})


def test_play_stops_with_a_repair_request_when_the_planner_may_be_asked() -> None:
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    spec = PlannerExecuterArchSpec(
        no_action_grace_seconds=0.2, state_source="pulse_memory", repair_after=2, repair="planner", max_failures_in_a_row=5
    )

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(SLOW, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(
            1, "Press Start.", macros, None, can_ask_planner=True
        )

    play = asyncio.run(go())

    assert play.repair_request == {"action": "press_start", "misses": 2, "reason": play.turns[-1]["reason"]}
    assert play.stopped.startswith("REPAIR: `press_start` failed 2 times in this step")
    reason, lines = repair_brief(play)
    assert "The action `press_start` failed 2 times in this step" in reason and "Its first failure: turn 0" in reason
    assert "Game observations from PulseMemory" in reason and lines and lines[-1].startswith("turn 1: press_start → failed")


def test_the_runner_has_the_planner_rewrite_the_step_and_the_evaluator_reads_both_plans() -> None:
    fake = _slow_menu()

    def to_map(game: FakeGame) -> None:
        game.scene = "Map"
        game.objects = {"Root/Stage": {"text": "Stage 0"}}

    # the first plan stalls; the planner's rewrite replaces the stuck action and keeps the rest
    rewrite = (
        '```changes\n{"base": 1, "replace": {"press_start": "it waits for a shop that never opens"}}\n```\n'
        "```python\n" + MACROS.split("\n\n\ndef look_around")[0] + "\n```\n"
    )
    asked = Verbatim("```python\n" + SLOW + "```", rewrite)

    # 두 번째 계획에서야 Start 가 지도를 연다.
    original = fake.reactions.copy()
    presses = {"n": 0}

    def press(game: FakeGame) -> None:
        presses["n"] += 1
        if presses["n"] > 2:
            to_map(game)

    fake.reactions[("press", "Root/Start")] = press
    judge, seen = _judge({"passed": True, "reason": "The map opened.", "state_detail": "", "insight": ""})

    state, failure = _run(
        fake, asked, judge, FakeJev(), repair_after=2, repair="planner", max_failures_in_a_row=5, analysis="off"
    )

    assert original == {} and failure is None
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED"
    # planner 는 두 번 불렸다: 처음 계획, 그리고 실패 기록을 읽고 다시 쓴 계획.
    assert len(asked.calls) == 2
    assert "need a new plan" in asked.calls[1][1] and "The action `press_start` failed 2 times" in asked.calls[1][1]
    # editor 는 부르지 않는다.
    assert not any(f["payload"].get("kind") == "macro_fix" for f in fake.frames("LOG"))
    # evaluator 는 두 계획의 turn 을 계획 번호와 함께 읽는다.
    user = seen[0][1]
    assert "plan 1 · turn 0" in user and "plan 2 · turn 0" in user
    # 다시 쓴 계획의 결정 로그에 전이가 남는다: 이유, 몇 번째인지와 예산, 남은 시간(묶음 2).
    decision = next(f["payload"]["decision"] for f in fake.frames("LOG") if f["payload"].get("kind") == "replan_macros")
    transition = decision["transition"]
    assert transition["trigger"] == "repair" and transition["attempt"] == 1 and transition["budget"] == 2
    assert transition["stopped"].startswith("REPAIR: `press_start` failed 2 times")
    assert "state_version" in transition and transition["seconds_left"] > 0


def test_merged_plays_keep_every_turn_and_reading_with_its_plan() -> None:
    first = StepPlay(
        "REPAIR: x", turns=[{"turn": 0, "picked": "a"}], seconds=10.0, start_screen="s1", end_screen="e1",
        evidence=[{"at": "start", "values": {"hp": 30}}, {"at": "end", "values": {"hp": 30}}], evidence_unknown=["X.y", "Z.w"],
    )
    second = StepPlay(
        "done", turns=[{"turn": 0, "picked": "b"}], seconds=5.0, start_screen="s2", end_screen="e2",
        evidence=[{"at": "start", "values": {"hp": 30}}, {"at": "end", "values": {"hp": 0}}], evidence_unknown=["X.y"],
    )

    merged = merge_plays([first, second])

    assert [(turn["plan"], turn["picked"]) for turn in merged.turns] == [(1, "a"), (2, "b")]
    assert [entry["at"] for entry in merged.evidence] == ["plan 1 · start", "plan 1 · end", "plan 2 · start", "plan 2 · end"]
    assert merged.stopped == "done" and merged.seconds == 15.0
    assert merged.start_screen == "s1" and merged.end_screen == "e2" and merged.evidence_unknown == ["X.y"]


def test_재계획을_합친_PulseMemory_관측도_판정에_전달() -> None:
    from app.agents.evaluator.verdict import evidence_text

    first = StepPlay("REPAIR: x", start_screen="hp: 30", end_screen="hp: 15", state_source="pulse_memory")
    last = StepPlay("done", start_screen="hp: 15", end_screen="hp: 0", state_source="pulse_memory")

    text = evidence_text(merge_plays([first, last]))

    assert "Game observations from PulseMemory" in text
    assert "Screen before play began:\nhp: 30" in text
    assert "Screen when play stopped:\nhp: 0" in text
    assert merge_plays([first]) is first


# Start 를 누르면 입력은 가고 macro 도 통과하지만 화면은 그대로다. 런 169 step 4 의 턴 종료 반복과 같은 꼴이다.
PATIENT = MACROS.replace('wait_until(scene() == "Map", 1, "Start did not open the map.")', 'wait_until(scene() == "Menu", 1, "Not on the menu.")')


def _repeat_play(can_ask_planner: bool) -> StepPlay:
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", repair="planner")

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(PATIENT, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(
            1, "Press Start.", macros, None, can_ask_planner=can_ask_planner
        )

    return asyncio.run(go())


def test_without_a_repair_left_an_action_picked_six_times_in_a_row_is_dropped_and_play_goes_on() -> None:
    """복구 예산이 없으면 멈추지 않고 그 action 을 빼며, 남은 action 이 다 빠지면 그때 멈춘다(개선 계획 묶음 2)."""
    play = _repeat_play(can_ask_planner=False)

    picked = [turn.get("picked") for turn in play.turns]
    assert picked[:6] == ["press_start"] * 6 and play.turns[5]["dropped"].startswith("`press_start` was picked 6 times")
    assert "press_start" not in picked[6:] and "look_around" in picked[6:]
    assert play.stopped.startswith("no action left:") and "(press_start, look_around)" in play.stopped


def test_the_same_action_picked_six_times_in_a_row_asks_the_planner_with_what_was_hidden() -> None:
    play = _repeat_play(can_ask_planner=True)
    play.turns[-1]["jev_state"] = {"hidden": {"objects": {"MeleeRock(Clone)": 3, "Word": 3}, "statics": ["Player.Hp"]}}

    assert play.repair_request == {
        "action": "press_start", "misses": None, "reason": "`press_start` was picked 6 times in a row without the step moving on",
    }
    reason, _ = repair_brief(play)
    assert reason.startswith("`press_start` was picked 6 times in a row without the step moving on.")
    assert "Hidden from the decision model by your ```observe block when play stopped: objects MeleeRock(Clone) ×3, Word ×3; static values Player.Hp" in reason



def test_an_effect_predicate_is_parsed_per_action_and_a_wrong_name_is_refused() -> None:
    import asyncio

    import pytest

    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.agents.qa.macro.errors import MacroRejection

    fake = _slow_menu()
    with_effect = MACROS + 'def effect_press_start() -> bool:\n    return scene() == "Map"\n'
    macros = asyncio.run(parse_step(with_effect, RegisteredMacros(fake.channel)))
    assert list(macros.effects) == ["press_start"]
    wrong = MACROS + 'def effect_press_stat() -> bool:\n    return True\n'
    with pytest.raises(MacroRejection, match="matches no action"):
        asyncio.run(parse_step(wrong, RegisteredMacros(fake.channel)))


def test_an_effect_that_does_not_show_is_recorded_and_counted_toward_a_repair() -> None:
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer, recent_action_lines
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    # Start 는 입력을 보내고 통과하지만 Map 은 열리지 않는다. 효과 조건이 그것을 잡는다.
    source = PATIENT + 'def effect_press_start() -> bool:\n    return scene() == "Map"\n'
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", repair="planner", repair_after=2)

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(
            1, "Press Start.", macros, None, can_ask_planner=True
        )

    play = asyncio.run(go())

    assert [turn.get("effect") for turn in play.turns] == ["unmet", "unmet"]
    # 입력 없이 끝난 것이 아니므로 첫 unmet 부터 센다.
    assert play.repair_request == {
        "action": "press_start", "misses": 2, "reason": "it passed and sent its input, but its effect did not show",
    }
    assert recent_action_lines(play.turns)[0].endswith("sent 1 input to the game; its effect did not show")


def test_the_evaluator_reads_the_earlier_verdicts_of_the_run() -> None:
    from app.agents.evaluator.verdict import request
    from app.agents.planner_executer.runner import earlier_verdicts
    from app.agents.qa.tools import QaRunState
    from app.qa.schemas import QaScenario, QaStep, QaStepResult

    scenario = QaScenario(title="WV", steps=[QaStep(action="튜토리얼을 끝까지 완료"), QaStep(action="1 스테이지 클리어")])
    state = QaRunState(total_steps=2)
    state.step_results.append(QaStepResult(step=1, passed=True, message="GameClearScene 을 거쳐 지도로 돌아왔다."))

    lines = earlier_verdicts(scenario, state)

    assert lines == ["step 1 (튜토리얼을 끝까지 완료): PASSED — GameClearScene 을 거쳐 지도로 돌아왔다."]
    user = request(scenario, 2, StepPlay("done"), lines)
    assert user.startswith("Earlier steps of this run and their verdicts:\n  step 1 (튜토리얼을 끝까지 완료): PASSED")



def test_nothing_fitting_for_the_grace_period_asks_the_planner() -> None:
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.05, state_source="pulse_memory", repair="planner")

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev(picks=("none_fits",)))).play(
            1, "Press Start.", macros, None, can_ask_planner=True
        )

    play = asyncio.run(go())

    assert play.stopped.startswith("REPAIR: no action fit")
    assert play.repair_request["action"] is None
    reason, _ = repair_brief(play)
    assert reason.startswith("The decision model found no listed action that fits the screen")



def test_the_after_block_is_read_and_a_wrong_name_is_refused() -> None:
    import pytest

    from app.agents.planner_executer.planner import read_after, split_after_block
    from app.agents.qa.macro.errors import MacroRejection

    rest, text = split_after_block('```python\npass\n```\n```after\n{"a": ["b"]}\n```')
    assert "after" not in rest
    assert read_after(text, ["a", "b"]) == {"a": ("b",)}
    assert read_after('{"a": "b"}', ["a", "b"]) == {"a": ("b",)}
    with pytest.raises(MacroRejection, match="not an action of this step"):
        read_after('{"a": ["c"]}', ["a", "b"])


def test_after_an_action_passes_the_next_turn_offers_only_what_follows_it() -> None:
    import asyncio
    import dataclasses

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", max_turns_per_step=2)
    jev = FakeJev(picks=("press_start", "press_start"))

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(PATIENT, RegisteredMacros(fake.channel))
        macros = dataclasses.replace(macros, after={"press_start": ("look_around",)})
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Press Start.", macros)

    play = asyncio.run(go())

    first, second = jev.asked("choice")
    assert "press_start" in first["questions"]["answer"]["criteria"]
    assert "press_start" not in second["questions"]["answer"]["criteria"]
    assert play.turns[1]["after"] == ["look_around"] and play.turns[1]["picked"] == "look_around"


GATED = MACROS.replace(
    '    start: object = find(name="Start")\n    click(start)\n',
    '    require(absent(selector("Root/Ghost")), "A ghost covers the menu.")\n'
    '    start: object = find(name="Start")\n    click(start)\n',
)


def test_an_action_a_truncated_reading_cannot_decide_is_unobserved_not_failed() -> None:
    """잘린 reading 에서 `absent()` 가 거절한 action 은 실패가 아니다. 다음 reading 을 기다리고, 이어지면 그때 실패로 센다
    (개선 계획 묶음 3, 런 182)."""
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import MAX_UNOBSERVED_IN_A_ROW, Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    fake = _slow_menu()
    fake.gaps = ["holder-limit:2"]
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", max_failures_in_a_row=2)

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(GATED, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(1, "Press Start.", macros)

    play = asyncio.run(go())

    outcomes = [turn.get("outcome") for turn in play.turns]
    assert outcomes[: MAX_UNOBSERVED_IN_A_ROW - 1] == ["unobserved"] * (MAX_UNOBSERVED_IN_A_ROW - 1)
    assert "truncated" in play.turns[0]["reason"] and not play.turns[0].get("misses")
    # 이어진 거절은 상한에서 실패로 세어, 실패 두 번 연속 규칙이 그 뒤에야 멈춘다.
    assert outcomes[MAX_UNOBSERVED_IN_A_ROW - 1] == "failed"
    assert play.stopped.startswith("2 actions failed in a row")


def test_a_selector_the_game_never_reported_is_named_and_a_reported_one_is_not() -> None:
    """런 184: developer macro 가 SDK 가 싣지 않는 객체(`PushArea1`)를 기다려 매번 시간 초과로 실패했다."""
    from types import SimpleNamespace

    from app.agents.planner_executer.executer import unseen_selectors

    pulse = SimpleNamespace(
        held={
            "a": SimpleNamespace(selector="CombineSystem/CombineZone", path="CombineSystem[15]/CombineZone[1]"),
            "b": SimpleNamespace(selector=None, path="CombineSystem[15]/CombineZone[1]/Zone1[0]"),
        },
        gone_at={"TurnBattleScene/RangedCat(Clone)[19]": 3},
    )
    reason = (
        'WAIT_TIMEOUT at x step 7: `wait_until(exists(selector("CombineSystem/CombineZone/PushArea1")), 3, "…")` '
        'and selector("CombineSystem/CombineZone") and selector("RangedCat(Clone)")'
    )

    assert unseen_selectors(reason, pulse) == ["CombineSystem/CombineZone/PushArea1"]
    assert unseen_selectors("no selector here", pulse) == []


def test_the_repair_brief_names_unseen_objects_and_shows_the_broken_developer_macro() -> None:
    from types import SimpleNamespace

    play = StepPlay(
        "REPAIR: stuck",
        repair_request={"action": "developer:open_window#2", "misses": 3, "reason": "WAIT_TIMEOUT"},
        unseen_selectors=["Root/Window/Pane"],
    )
    developer = SimpleNamespace(usable={"open_window": SimpleNamespace(macro=SimpleNamespace(source="def open_window() -> None:\n    pass"))})

    reason, _ = repair_brief(play, developer)

    assert "never reported in this run: Root/Window/Pane" in reason and "the macro is wrong, not the game" in reason
    assert "The source of the developer macro `open_window`:" in reason and "def open_window() -> None:" in reason
    assert "replace it in the ```changes block with your own `def`" in reason


def test_with_a_repair_left_failures_in_a_row_ask_the_planner_instead_of_stopping() -> None:
    import asyncio

    from app.agents.planner_executer.arch import PlannerExecuterArchSpec
    from app.agents.planner_executer.executer import Executer
    from app.agents.planner_executer.macros import RegisteredMacros, parse_step
    from app.qa.jev import JevClient

    failing = MACROS.replace('wait_until(scene() == "Map", 1,', 'wait_until(scene() == "Nowhere", 0.2,')
    fake = _slow_menu()
    spec = PlannerExecuterArchSpec(
        no_action_grace_seconds=0.2, state_source="pulse_memory", repair="planner", max_failures_in_a_row=2, repair_after=5
    )

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(failing, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=FakeJev())).play(
            1, "Press Start.", macros, None, can_ask_planner=True
        )

    play = asyncio.run(go())

    assert play.repair_request is not None and play.repair_request["reason"].startswith("2 actions failed in a row")
    assert play.stopped.startswith("REPAIR: 2 actions failed in a row")


def test_the_after_block_reads_a_developer_call_as_its_action() -> None:
    from app.agents.planner_executer.planner import read_after

    actions = ["developer:cast", "developer:select", "developer:select#2", "developer:end_turn"]
    after = read_after('{"developer:cast": ["developer:select(find(name=\\"Cat\\"))"]}', actions)

    assert after == {"developer:cast": ("developer:select", "developer:select#2")}
