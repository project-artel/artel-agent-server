"""The planner writes a step's macros, the executer plays them with Jev, the evaluator judges.

Everything runs over `FakeGame`, a fake model (`Asked`, `judge`) and a fake Jev endpoint
(`FakeJev` as `JevClient`'s `post`). Nothing reaches the network.
"""

import asyncio
import itertools
import json

import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.evaluator import Evaluator, request
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.planner_executer.planner import Planner
from app.agents.qa.macro.errors import MacroRejection
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.arch import QaArchSpec
from app.qa.jev import JevClient
from app.qa.run_config import resolve_run_config
from app.qa.scene_context import SceneCapability, SceneContext, SceneContextEntry
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame

# Menu 의 Start 를 누르면 Map 으로 간다.
MACROS = '''def press_start() -> None:
    """Press Start on the title screen. The right move while the menu shows Start."""
    start: object = find(name="Start")
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")


def look_around() -> None:
    """Wait for the screen to settle. The right move while nothing can be pressed yet."""
    wait_until(scene() == "Menu", 1, "Not on the menu.")


def done() -> bool:
    return scene() == "Map"
'''


class FakeJev:
    """`JevClient` 의 `post` 자리. 질문 종류마다 정해 둔 답을 차례로 내고 받은 본문을 남긴다."""

    def __init__(self, picks=("press_start",), goal=(0.9,)) -> None:
        self.picks = itertools.chain(picks, itertools.repeat(picks[-1])) if picks else None
        self.goal = itertools.chain(goal, itertools.repeat(goal[-1])) if goal else None
        self.bodies: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        if body["questions"]["answer"]["type"] == "choice":
            return {"answers": {"answer": {"choice": next(self.picks)}}}
        return {"answers": {"answer": {"noul": next(self.goal)}}}

    def asked(self, kind: str) -> list[dict]:
        return [body for body in self.bodies if body["questions"]["answer"]["type"] == kind]


class Asked:
    """The planner's model: answers in turn, and records what it was sent."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return f"```python\n{self.answers.pop(0)}```"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _quick(monkeypatch) -> None:
    monkeypatch.setattr(executer_module, "SETTLE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(executer_module, "IDLE_LOOK_SECONDS", 0.01)


def _menu() -> FakeGame:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})

    def to_map(game: FakeGame) -> None:
        game.scene = "Map"
        game.objects = {"Root/Stage": {"text": "Stage 0"}}

    fake.reactions[("press", "Root/Start")] = to_map
    return fake


def _scenario() -> QaScenario:
    return QaScenario(
        title="Start",
        steps=[QaStep(action="Press Start.", case_id=5, case=QaCaseRef(id=5, expected="The map opens."))],
    )


def _config(**knobs):
    return resolve_run_config(
        arch=QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(**knobs))
    )


def _play(fake: FakeGame, jev: FakeJev, source: str = MACROS, clock=None, failure_memory=None, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(**{"no_action_grace_seconds": 0.2, **knobs})

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        executer = Executer(fake.channel, spec, JevClient(post=jev), **({"clock": clock} if clock else {}))
        return await executer.play(1, "Press Start.", macros, failure_memory)

    return asyncio.run(go())


def _turn_logs(fake: FakeGame) -> list[dict]:
    return [f["payload"]["turn"] for f in fake.frames("LOG") if f["payload"].get("kind") == "turn"]


# --- the planner -------------------------------------------------------------------------------


def _write(fake: FakeGame, asked: Asked, **knobs):
    config = _config(**knobs)

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=asked)
        return await planner.write(_scenario(), 1)

    return asyncio.run(go())


def test_the_planner_writes_macros_from_the_rules_the_grammar_the_map_and_the_screen() -> None:
    from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY

    fake = _menu()
    fake.channel.scene.scene_context = SceneContext(
        scenes=[
            SceneContextEntry(
                scene_name="Menu",
                capabilities=[SceneCapability(interaction="click", control_label="Start", summary="starts a game")],
            ),
            SceneContextEntry(scene_name="Map"),
        ]
    )
    asked = Asked(MACROS)

    written = _write(fake, asked)

    assert written.refusal is None and list(written.macros.actions) == ["press_start", "look_around"]
    assert written.macros.description("press_start").startswith("Press Start on the title screen.")
    system, user = asked.calls[0]
    assert system.startswith("You write ONE test step of a game as macros.")
    assert GRAMMAR in system and system.rstrip().endswith(NUMBER_EQUALITY)
    assert "Scenes in this build" in user and '- click "Start" — starts a game' in user
    assert '"verify": "The map opens."' in user
    assert "Current screen:" in user and "Root/Start" in user


@pytest.mark.parametrize(
    ("refused", "said"),
    [
        (MACROS.replace("def done() -> bool:\n    return scene() == \"Map\"\n", ""), "`def done() -> bool:`"),
        (MACROS.replace('    """Wait for the screen to settle. The right move while nothing can be pressed yet."""\n', ""),
         "`def look_around` has no docstring"),
        (MACROS.replace('wait_until(scene() == "Map", 1,', 'require(scene() == "Map" and exists(start),'),
         "MACRO_NODE_REJECTED"),
        (MACROS.replace("def look_around() -> None:", "def look_around(times: int) -> None:"), "takes parameters"),
    ],
)
def test_a_refused_answer_goes_back_with_the_refusal_and_the_rewrite_is_used(refused: str, said: str) -> None:
    asked = Asked(refused, MACROS)

    written = _write(_menu(), asked)

    assert written.macros is not None
    assert said in asked.calls[1][1] and "Your previous answer cannot be used as written. It was refused:" in asked.calls[1][1]


def test_a_second_rewrite_is_told_every_refusal_so_far() -> None:
    """직전 거절만 돌려주면 하나를 고치며 앞서 고친 실수를 되살린다(로컬 try 167, ARTEL-1189)."""
    no_goal = MACROS.replace("def done() -> bool:\n    return scene() == \"Map\"\n", "")
    no_doc = MACROS.replace('    """Wait for the screen to settle. The right move while nothing can be pressed yet."""\n', "")
    asked = Asked(no_goal, no_doc, MACROS)

    written = _write(_menu(), asked)

    assert written.macros is not None
    third = asked.calls[2][1]
    assert "Your previous answers cannot be used as written. Each was refused:" in third
    assert "  1. " in third and "`def done() -> bool:`" in third
    assert "  2. " in third and "`def look_around` has no docstring" in third


def test_an_answer_still_refused_after_the_rewrites_is_returned_with_its_refusal() -> None:
    no_goal = MACROS.replace("def done() -> bool:", "def finished() -> bool:")
    asked = Asked(no_goal, no_goal)

    written = _write(_menu(), asked, max_rewrites=1)

    assert len(asked.calls) == 2
    assert written.macros is None and "`def done() -> bool:`" in written.refusal


def test_an_import_resolves_against_the_builds_registered_macros() -> None:
    fake = _menu()
    fake.macros = {"tap_start": 'def tap_start() -> None:\n    start: object = find(name="Start")\n    click(start)\n'}
    importing = "from macros import tap_start\n\n" + MACROS.replace(
        '    start: object = find(name="Start")\n    click(start)\n', "    tap_start()\n"
    )

    written = _write(fake, Asked(importing))

    assert written.refusal is None
    assert [f["payload"]["name"] for f in fake.frames("MACRO_READ")] == ["tap_start"]


# --- the executer --------------------------------------------------------------------------------


def test_jev_picks_an_action_and_confirms_the_goal_before_play_stops() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    play = _play(fake, jev)

    assert play.stopped == "done"
    (pick,) = jev.asked("choice")
    question = pick["questions"]["answer"]
    assert question["instructions"] == f"{executer_module.PICK_QUESTION} The step: Press Start."
    # Jev 는 docstring 의 첫 줄만 읽는다. 기다림은 마지막이라고 적힌 선택지다.
    assert question["criteria"] == {
        "press_start": {"what": "Press Start on the title screen. The right move while the menu shows Start."},
        "look_around": {"what": "Wait for the screen to settle. The right move while nothing can be pressed yet."},
        "none_fits": {"what": executer_module.WAIT_DESCRIPTION},
    }
    assert pick["state"] == {
        "scene": "Menu",
        "objects": [{"path": "Root/Start", "text": "Start", "members": {}}],
        "recent_picks": [],
    }
    (goal,) = jev.asked("noul")
    assert goal["questions"]["answer"]["instructions"] == "Has this step's goal been reached? The step: Press Start."
    assert goal["state"]["scene"] == "Map"
    first, second = play.turns
    assert first["done"] is False and first["picked"] == "press_start" and first["outcome"] == "passed"
    assert first["sent"][0]["summary"] == "click(start)"
    assert second["done"] is True and second["goal_ask"] == 0.9 and "picked" not in second
    assert [log["turn"] for log in _turn_logs(fake)] == [0, 1]


def test_the_action_jev_names_is_the_one_that_runs() -> None:
    fake = _menu()

    play = _play(fake, FakeJev(picks=("look_around", "press_start")))

    assert [turn.get("picked") for turn in play.turns] == ["look_around", "press_start", None]
    assert play.stopped == "done"


def test_nothing_fitting_waits_for_the_screen_and_stops_after_the_grace() -> None:
    fake = _menu()

    play = _play(fake, FakeJev(picks=("none_fits",)))

    assert fake.actions == []
    assert play.stopped == "no action fit for 0.2s"
    assert all(turn["picked"] == "none_fits" for turn in play.turns)


def test_when_jev_does_not_confirm_the_goal_play_goes_on_and_then_stops_on_the_disagreement() -> None:
    fake = FakeGame("Map", {"Root/Stage": {"text": "Stage 0"}})  # `done()` 이 처음부터 참이다

    play = _play(fake, FakeJev(picks=("look_around",), goal=(0.2,)), max_done_disagreements=2)

    assert [turn["goal_ask"] for turn in play.turns] == [0.2, 0.2]
    assert play.turns[0]["picked"] == "look_around"  # 맞지 않으면 play 를 이어 간다
    assert play.stopped.startswith("done() and Jev disagree")


def test_actions_failing_in_a_row_stop_play() -> None:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})  # 눌러도 아무 일이 없다

    play = _play(fake, FakeJev(), max_failures_in_a_row=2)

    assert [turn["outcome"] for turn in play.turns] == ["failed", "failed"]
    assert play.turns[0]["reason"].startswith("WAIT_TIMEOUT at press_start step 3 of 3: ")
    assert play.stopped.startswith("2 actions failed in a row (last: press_start: WAIT_TIMEOUT")


def test_an_action_that_keeps_failing_without_sending_is_dropped_between_successes() -> None:
    """성공한 action 이 사이에 끼면 연속 실패 수가 0 으로 돌아가 상한에 안 걸린다(로컬 try 166, ARTEL-1189).

    아무것도 못 보낸 실패는 이름으로 따로 세어, 같은 씬에서 두 번이면 선택지에서 뺀다.
    """
    fake = FakeGame("Menu", {"Root/Title": {"text": "Title"}})  # Start 가 없어 press_start 는 아무것도 못 보낸다

    play = _play(
        fake,
        FakeJev(picks=("press_start", "look_around", "press_start", "look_around", "look_around")),
        max_turns_per_step=5,
    )

    outcomes = [(turn["picked"], turn["outcome"]) for turn in play.turns]
    assert outcomes[:4] == [
        ("press_start", "failed"), ("look_around", "passed"), ("press_start", "failed"), ("look_around", "passed"),
    ]
    assert play.turns[0]["reason"].startswith("SELECTOR_NOT_FOUND")
    assert "press_start" in play.turns[2]["options"]
    assert play.turns[4]["dropped"] == ["press_start"]
    assert play.turns[4]["options"] == ["look_around"]


def test_play_stops_when_every_action_failed_without_sending() -> None:
    fake = FakeGame("Other", {"Root/Title": {"text": "Title"}})  # 두 action 모두 이 화면에서 아무것도 못 보낸다

    play = _play(
        fake, FakeJev(picks=("press_start", "look_around", "press_start", "look_around")), max_failures_in_a_row=10
    )

    assert play.stopped == (
        "every action kept failing, left the screen as it was, or ran out of picks here: look_around, press_start"
    )
    assert play.turns[-1]["dropped"] == ["look_around", "press_start"]


def test_an_action_that_sends_and_keeps_failing_is_dropped_after_three() -> None:
    """입력을 보냈는데 실패한 것도 같은 씬에서 세 번이면 뺀다(로컬 try 169 의 close_combine_zone)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})  # 눌러도 아무 일이 없다: 보내고 시간 초과

    play = _play(
        fake,
        FakeJev(picks=("press_start", "look_around", "press_start", "look_around", "press_start", "look_around")),
        max_turns_per_step=7,
    )

    picks = [(turn["picked"], turn["outcome"]) for turn in play.turns[:6]]
    assert picks.count(("press_start", "failed")) == 3
    assert play.turns[0]["sent"]  # 보낸 실패다
    # 입력 없이 통과하는 `look_around` 도 세 번이면 빠진다(try 191).
    assert play.turns[6]["dropped"] == ["look_around", "press_start"]


def test_a_click_whose_aim_the_game_refused_counts_as_sending_nothing() -> None:
    """SDK 는 조준이 거절되면 누름을 돌리지 않는다. 그런 실패는 두 번이면 뺀다(로컬 try 176 의 skip_tutorial)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})
    fake.refuse.add(("move_mouse", "Root/Start"))

    play = _play(fake, FakeJev(picks=("press_start", "press_start", "look_around")), max_turns_per_step=3, max_failures_in_a_row=5)

    assert [turn["outcome"] for turn in play.turns[:2]] == ["failed", "failed"]
    assert play.turns[0]["sent"]  # 묶음은 보냈다. 게임이 받지 않았다
    assert play.turns[2]["dropped"] == ["press_start"]


NUDGE = '''def nudge() -> None:
    """Press the right arrow to move on the map."""
    press_key("RightArrow")


def look_around() -> None:
    """Wait for the screen to settle."""
    wait_until(scene() == "Menu", 1, "Not on the menu.")


def done() -> bool:
    return scene() == "Map"
'''


def test_an_action_that_passes_but_leaves_the_screen_as_it_was_is_dropped_after_three() -> None:
    """키를 보내고 통과했는데 화면이 그대로인 action 은 같은 씬에서 세 번 연달아면 뺀다(로컬 try 172 의 move_on_map)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})  # 방향키에 반응하는 것이 없다

    play = _play(fake, FakeJev(picks=("nudge",)), source=NUDGE, max_turns_per_step=5)

    assert [(turn["picked"], turn["outcome"], turn.get("unchanged")) for turn in play.turns[:3]] == [
        ("nudge", "passed", True)
    ] * 3
    assert play.turns[3]["dropped"] == ["nudge"]
    assert play.turns[3]["options"] == ["look_around"]


def test_an_action_that_changes_the_screen_is_not_counted_as_unchanged() -> None:
    fake = FakeGame("Menu", {"Root/Count": {"text": "0"}})

    def count(game: FakeGame) -> None:
        game.objects["Root/Count"]["text"] = str(int(game.objects["Root/Count"]["text"]) + 1)

    fake.reactions[("key_click", "RightArrow")] = count

    play = _play(fake, FakeJev(picks=("nudge",)), source=NUDGE, max_turns_per_step=5)

    assert [turn.get("unchanged") for turn in play.turns] == [None] * 5
    assert "dropped" not in play.turns[-1]


PRESS_LABEL = '''def press_label() -> None:
    """Press the Start label."""
    label: object = selector("Root/Start/Label")
    click(label)


def look_around() -> None:
    """Wait for the screen to settle."""
    wait_until(scene() == "Menu", 1, "Not on the menu.")


def done() -> bool:
    return scene() == "Map"
'''


def test_an_action_whose_presses_reach_nothing_is_dropped_after_three_even_while_the_screen_moves() -> None:
    """전투처럼 화면이 늘 움직여도, 누름이 매번 빈 자리에 떨어진 action 은 뺀다(로컬 try 180 의 카드 글자 drag)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}, "Root/Start/Label": {"text": "Start"}, "Root/Clock": {"text": "0"}})
    fake.unreachable.add("Root/Start/Label")

    def tick(game: FakeGame) -> None:
        game.objects["Root/Clock"]["text"] = str(int(game.objects["Root/Clock"]["text"]) + 1)

    fake.reactions[("press", "Root/Start/Label")] = tick

    play = _play(fake, FakeJev(picks=("press_label",)), source=PRESS_LABEL, max_turns_per_step=5)

    assert [(turn["outcome"], turn.get("missed"), turn.get("unchanged")) for turn in play.turns[:3]] == [
        ("passed", True, None)
    ] * 3
    assert play.turns[3]["dropped"] == ["press_label"]


def test_an_action_picked_too_often_in_one_play_is_dropped(monkeypatch) -> None:
    """실패 없이 통과하며 진전이 없는 action 은 다른 상한에 안 걸린다(로컬 try 170 의 toggle_combine_panel 200번)."""
    monkeypatch.setattr(executer_module, "MAX_PICKS_PER_ACTION", 3)

    play = _play(_menu(), FakeJev(picks=("look_around", "look_around", "look_around", "press_start")))

    assert [turn.get("picked") for turn in play.turns[:4]] == ["look_around"] * 3 + ["press_start"]
    assert play.turns[3]["dropped"] == ["look_around"]
    assert play.turns[3]["options"] == ["press_start"]
    assert play.stopped == "done"


def test_a_failure_memory_carries_drops_into_the_next_case_of_the_plan() -> None:
    """같은 계획의 case 들은 action 을 나눠 쓴다. 앞 case 에서 빠진 action 은 다음 case 에서도 빠진다(로컬 try 171)."""
    fake = FakeGame("Menu", {"Root/Title": {"text": "Title"}})  # Start 가 없어 press_start 는 아무것도 못 보낸다
    memory = executer_module.FailureMemory()

    _play(fake, FakeJev(picks=("press_start", "look_around", "press_start", "look_around")),
          max_turns_per_step=4, failure_memory=memory)
    second = _play(fake, FakeJev(picks=("look_around",)), max_turns_per_step=1, failure_memory=memory)

    assert memory.sendless == {"press_start": 2}
    assert second.turns[0]["dropped"] == ["press_start"]


def test_play_stops_at_the_turn_cap() -> None:
    play = _play(_menu(), FakeJev(picks=("look_around",)), max_turns_per_step=2)

    assert len(play.turns) == 2 and play.stopped == "turn limit"


def test_play_stops_at_the_time_budget() -> None:
    clock = Clock()

    class Later(FakeJev):
        async def __call__(self, body: dict) -> dict:
            clock.now += 10
            return await super().__call__(body)

    play = _play(_menu(), Later(picks=("look_around",)), clock=clock, step_budget_seconds=15)

    assert play.stopped == "step budget 15s" and len(play.turns) == 2


def test_a_jev_error_stops_play_with_the_error() -> None:
    fake = _menu()

    async def down(body: dict) -> dict:
        raise RuntimeError("decisions endpoint down")

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, PlannerExecuterArchSpec(), JevClient(post=down)).play(
            1, "Press Start.", macros
        )

    play = asyncio.run(go())

    assert play.stopped.startswith("JEV-ERROR: Jev request failed: decisions endpoint down")
    assert fake.actions == []


# --- the evaluator -----------------------------------------------------------------------------


PLAYED = StepPlay(
    "done",
    turns=[
        {"step": 1, "turn": 0, "scene": "Menu", "done": False, "picked": "press_start", "outcome": "passed"},
        {"step": 1, "turn": 1, "scene": "Map", "done": True, "goal_ask": 0.9},
    ],
    start_screen="(menu screen)",
    end_screen="(map screen)",
)


def _judge(*answers):
    seen: list[tuple[str, str]] = []
    queue = list(answers)

    async def judge(system: str, user: str):
        seen.append((system, user))
        return queue.pop(0)

    return judge, seen


def test_the_evaluator_reads_the_step_the_turns_and_both_screens() -> None:
    judge, seen = _judge({"passed": True, "reason": "The map scene shows Stage 0."})

    passed, message = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAYED))

    assert passed and message == "The map scene shows Stage 0."
    system, user = seen[0]
    assert system.startswith("You judge ONE test step of a game after it was played.")
    assert user == request(_scenario(), 1, PLAYED)
    assert '"verify": "The map opens."' in user and "Why play stopped: done" in user
    assert "turn 0 on Menu: done()=False; Jev picked press_start; → passed" in user
    assert "turn 1 on Map: done()=True; Jev on the goal being reached: 0.9" in user
    assert "Screen when the step began:\n(menu screen)" in user
    assert "Screen when play stopped:\n(map screen)" in user


def test_a_failed_verdict_names_why_play_stopped() -> None:
    judge, _ = _judge('{"passed": false, "reason": "The menu is still shown."}')
    stopped = StepPlay("no action fit for 3s", start_screen="a", end_screen="b")

    passed, message = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, stopped))

    assert not passed
    assert message == "The menu is still shown. (executer stopped: no action fit for 3s)"


def test_an_unreadable_verdict_is_asked_again_once() -> None:
    judge, seen = _judge("I think it passed", {"passed": True, "reason": "ok"})

    passed, _ = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAYED))

    assert passed and len(seen) == 2


def test_two_unreadable_verdicts_fail_the_step_with_evaluator_error() -> None:
    judge, _ = _judge("no", '{"passed": "maybe"}')

    passed, message = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, PLAYED))

    assert not passed and message.startswith("EVALUATOR-ERROR: no readable verdict in 2 answers")


# --- the whole step ------------------------------------------------------------------------------


def _run(fake: FakeGame, asked: Asked, judge, jev: FakeJev, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, _scenario())

    return asyncio.run(go())


def _verdicts(fake: FakeGame) -> list[dict]:
    return [f["payload"] for f in fake.frames("STATUS") if f["payload"].get("step") is not None]


def test_one_step_is_written_played_judged_and_reported() -> None:
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map scene shows Stage 0."})

    state, failure = _run(fake, Asked(MACROS), judge, FakeJev())

    assert failure is None and state.finished
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and verdict["message"] == "The map scene shows Stage 0."
    assert verdict["case_id"] == 5 and verdict["is_verification"] is True
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED" and closing["summary"]["steps"]["passed"] == 1
    assert "Screen when play stopped:" in seen[0][1] and "Map" in seen[0][1].split("Screen when play stopped:")[1]
    (action,) = fake.frames("ACTION")
    assert action["payload"]["message"] == "click(start)"


def test_a_step_whose_macros_stay_refused_fails_without_play() -> None:
    fake = _menu()
    no_goal = MACROS.replace("def done() -> bool:", "def finished() -> bool:")
    judge, seen = _judge()

    _run(fake, Asked(no_goal, no_goal), judge, FakeJev(), max_rewrites=1)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith("PLANNER-REFUSED: the step's macros were still refused after 1 rewrites:")
    assert fake.actions == [] and seen == []


def test_turn_records_go_out_as_logs_with_the_jev_answers() -> None:
    fake = _menu()
    judge, _ = _judge({"passed": True, "reason": "ok"})

    _run(fake, Asked(MACROS), judge, FakeJev())

    first, second = _turn_logs(fake)
    assert first["picked"] == "press_start" and first["options"] == ["press_start", "look_around"]
    assert second["done"] is True and second["goal_ask"] == 0.9
    json.dumps(first)


FEASIBLE = '''def press_start() -> None:
    """Press Start on the title screen."""
    start: object = find(name="Start")
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")


def look_around() -> None:
    """Wait for the screen to settle."""
    wait_until(scene() == "Menu", 1, "Not on the menu.")


def can_look_around() -> bool:
    return scene() == "Loading"


def done() -> bool:
    return scene() == "Map"
'''


def test_an_action_whose_can_predicate_is_false_is_not_offered() -> None:
    """`can_<action>` 이 거짓이면 그 턴의 선택지에서 뺀다. Jev 는 할 수 없게 된 것을 스스로 빼지 못했다(try 184 재현)."""
    fake = _menu()
    jev = FakeJev()

    play = _play(fake, jev, source=FEASIBLE)

    assert play.stopped == "done"
    criteria = jev.asked("choice")[0]["questions"]["answer"]["criteria"]
    assert set(criteria) == {"press_start", "none_fits"}
    assert play.turns[0]["infeasible"] == ["look_around"]


def test_a_can_predicate_for_an_action_that_does_not_exist_is_refused() -> None:
    source = FEASIBLE.replace("def can_look_around", "def can_jump")
    fake = _menu()

    async def go():
        fake.push_reading()
        await parse_step(source, RegisteredMacros(fake.channel))

    with pytest.raises(MacroRejection, match="there is no action `jump`"):
        asyncio.run(go())


def test_when_nothing_can_be_done_the_turn_waits_without_asking_jev() -> None:
    source = FEASIBLE.replace('def done() -> bool:', 'def can_press_start() -> bool:\n    return scene() == "Loading"\n\n\ndef done() -> bool:')
    fake = _menu()
    jev = FakeJev()

    play = _play(fake, jev, source=source)

    assert jev.asked("choice") == []
    assert play.turns[0]["picked"] == "none_fits"
    assert play.stopped.startswith("no action fit")


def test_the_scores_jev_gives_each_option_are_kept_on_the_turn() -> None:
    class Scored(FakeJev):
        async def __call__(self, body: dict) -> dict:
            answer = await super().__call__(body)
            if body["questions"]["answer"]["type"] == "choice":
                answer["answers"]["answer"].update(
                    probabilities={"press_start": 0.81, "look_around": 0.12, "none_fits": 0.07}, confidence=0.69
                )
            return answer

    play = _play(_menu(), Scored())

    assert play.turns[0]["scores"] == {"press_start": 0.81, "look_around": 0.12, "none_fits": 0.07}
    assert play.turns[0]["confidence"] == 0.69


FEATURES = FEASIBLE.replace('def done() -> bool:', '''def feature_on_menu() -> bool:
    return scene() == "Menu"


def feature_start_shown() -> bool:
    start: object = selector("Root/Start")
    return exists(start)


def done() -> bool:''')


def test_when_the_plan_writes_features_jev_reads_them_before_the_object_list() -> None:
    """특징값은 화면 오브젝트 목록 앞에 보탠다(JEV-Star 의 구조화된 관측, try 185 재현). 대신하던 때는 planner 가
    빠뜨린 값이 어디에도 없었다(try 193)."""
    fake = _menu()
    jev = FakeJev()

    play = _play(fake, jev, source=FEATURES)

    state = jev.asked("choice")[0]["state"]
    assert state == {
        "scene": "Menu",
        "features": {"on_menu": True, "start_shown": True},
        "objects": [{"members": {}, "path": "Root/Start", "text": "Start"}],
        "recent_picks": [],
    }
    assert play.turns[0]["features"] == {"on_menu": True, "start_shown": True}


def test_a_feature_that_takes_parameters_is_refused() -> None:
    source = FEATURES.replace("def feature_on_menu() -> bool:", "def feature_on_menu(name: string) -> bool:")
    fake = _menu()

    async def go():
        fake.push_reading()
        await parse_step(source, RegisteredMacros(fake.channel))

    with pytest.raises(MacroRejection, match="must take no parameters"):
        asyncio.run(go())


TWO_CLICKS = '''def press_start_twice() -> None:
    """PRESS START — presses Start, then presses it again."""
    start: object = find(name="Start")
    click(start)
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")


def look_around() -> None:
    """WAIT — waits for the screen to settle."""
    wait_until(scene() == "Menu", 1, "Not on the menu.")


def done() -> bool:
    return scene() == "Map"
'''


def test_a_press_a_covering_panel_swallows_stops_the_action_and_is_not_counted_as_a_failure() -> None:
    """누름이 화면을 덮은 판에서 끝나면 그 자리에서 멈춘다. 실패로 세지 않고 무엇이 막았는지 최근 선택에 남긴다(try 187)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})
    fake.cover = "Tutorial/InputBlocker"
    jev = FakeJev(picks=("press_start_twice", "press_start_twice"))

    memory = executer_module.FailureMemory()
    play = _play(fake, jev, source=TWO_CLICKS, max_turns_per_step=2, failure_memory=memory)

    first = play.turns[0]
    assert first["outcome"] == "blocked"
    assert first["reason"].startswith("INPUT_BLOCKED")
    # 두 번째 click 은 보내지 않았다.
    assert [method for method, _ in fake.actions].count("mouse_down") == 2
    assert memory.failed == {} and memory.sendless == {}
    second_state = jev.asked("choice")[1]["state"]
    assert second_state["recent_picks"] == [
        {
            "action": "PRESS START — presses Start, then presses it again.",
            "outcome": "blocked by Tutorial/InputBlocker",
            "changed": "nothing the actions read",
        }
    ]


NOTHING_TO_DO = '''def close_dialogue() -> None:
    """CLOSE DIALOGUE — presses Space while a dialogue is open."""
    if exists(selector("Root/Dialogue")):
        press_key("Space")


def done() -> bool:
    return scene() == "Map"
'''


def test_an_action_that_passes_without_sending_anything_is_dropped_after_three() -> None:
    """입력 없이 통과하는 action 도 반복으로 센다. 대사가 없을 때의 대사 넘기기가 30번 돌았다(try 191)."""
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})

    play = _play(fake, FakeJev(picks=("close_dialogue",)), source=NOTHING_TO_DO, max_turns_per_step=5)

    assert [(turn["outcome"], turn.get("no_input")) for turn in play.turns[:3]] == [("passed", True)] * 3
    assert play.stopped.startswith("every action kept failing")
