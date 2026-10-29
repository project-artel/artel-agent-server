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


def _play(fake: FakeGame, jev: FakeJev, source: str = MACROS, clock=None, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(**{"no_action_grace_seconds": 0.2, **knobs})

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        executer = Executer(fake.channel, spec, JevClient(post=jev), **({"clock": clock} if clock else {}))
        return await executer.play(1, "Press Start.", macros)

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
        # 인자 있는 `-> None` def 는 helper 라 action 이 아니다. 둘 다 helper 가 되면 action 이 없다.
        (
            MACROS.replace("def look_around() -> None:", "def look_around(times: int) -> None:").replace(
                "def press_start() -> None:", "def press_start(times: int) -> None:"
            ),
            "at least one action",
        ),
    ],
)
def test_a_refused_answer_goes_back_with_the_refusal_and_the_rewrite_is_used(refused: str, said: str) -> None:
    asked = Asked(refused, MACROS)

    written = _write(_menu(), asked)

    assert written.macros is not None
    assert said in asked.calls[1][1] and "The macro parser refused it" in asked.calls[1][1]


def test_a_def_with_parameters_is_a_helper_the_actions_call_and_not_an_option() -> None:
    """developer macro 본문이 그렇게 쓰여 있다(`fill_spell(word: string)`). editor 가 그 본문을 재사용할 수
    있어야 하므로 helper 는 거절도 선택지도 아니다(ARTEL-1288)."""
    fake = _menu()
    with_helper = (
        MACROS.replace("    click(start)\n", "    press(start)\n")
        + "\n\ndef press(target: object) -> None:\n    click(target)\n"
    )

    async def go():
        fake.push_reading()
        return await parse_step(with_helper, RegisteredMacros(fake.channel))

    macros = asyncio.run(go())

    assert list(macros.actions) == ["press_start", "look_around"]
    assert "press" not in macros.actions


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
    assert question["instructions"] == (
        "Which of these should the player do now to progress this step? The step: Press Start."
    )
    assert question["criteria"] == {
        "press_start": {"what": "Press Start on the title screen. The right move while the menu shows Start."},
        "look_around": {"what": "Wait for the screen to settle. The right move while nothing can be pressed yet."},
        "none_fits": {"what": "None of the other options fits the current state."},
    }
    assert pick["state"] == {"scene": "Menu", "objects": [{"path": "Root/Start", "text": "Start", "members": {}}]}
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


def test_jev_is_told_what_the_last_action_turns_did_when_it_picks_again() -> None:
    fake = _menu()
    jev = FakeJev(picks=("look_around", "look_around", "press_start"))

    play = _play(fake, jev)

    first, second, third = jev.asked("choice")
    # 첫 turn 에는 지난 action 이 없어 state 가 전과 같다.
    assert "recent_actions" not in first["state"]
    assert second["state"]["recent_actions"] == ["turn 0: look_around → passed, sent no input to the game"]
    assert third["state"]["recent_actions"] == [
        "turn 0: look_around → passed, sent no input to the game",
        "turn 1: look_around → passed, sent no input to the game",
    ]
    assert play.turns[2]["recent_actions"] == third["state"]["recent_actions"]
    assert play.stopped == "done"


def test_recent_action_lines_carry_failures_flags_and_repairs_and_keep_the_last_three() -> None:
    turns = [
        {"turn": 0, "picked": "a", "outcome": "passed", "sent": [{"summary": "click(x)"}]},
        {"turn": 1, "done": True},  # `done()` 만 읽은 turn 은 빠진다
        {"turn": 2, "picked": "none_fits"},  # action 을 안 돌린 turn 도 빠진다
        {"turn": 3, "picked": "b", "outcome": "failed", "sent": [], "reason": "WAIT_TIMEOUT at b step 2 of 2"},
        {"turn": 4, "picked": "c", "outcome": "passed", "sent": [], "flags": [{"place": "c step 2", "message": "no card"}]},
        {"turn": 5, "picked": "c", "outcome": "passed", "sent": [], "repair": {"action": "c", "attempt": 1, "replaced": False}},
    ]

    assert executer_module.recent_action_lines(turns) == [
        "turn 3: b → failed, sent no input to the game (WAIT_TIMEOUT at b step 2 of 2)",
        "turn 4: c → passed, sent no input to the game; flag: no card",
        "turn 5: c → passed, sent no input to the game; a rewrite was refused after this",
    ]


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


# `press_start` 가 이 화면에 없는 가지만 들어 있다: 돌지만 게임에 아무것도 보내지 않는다.
STUCK = MACROS.replace(
    '''    start: object = find(name="Start")
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")
''',
    '''    if scene() == "Battle":
        start: object = find(name="Start")
        click(start)
''',
)


class Repairs:
    """The executer's `repair`: answers with the parsed sources in turn and records what it was asked."""

    def __init__(self, fake: FakeGame, *sources: str | None) -> None:
        self.fake = fake
        self.sources = list(sources)
        self.calls: list[tuple[list[str], str, list[dict]]] = []

    async def __call__(self, macros, action: str, turns: list[dict]):
        self.calls.append((list(macros.actions), action, turns))
        source = self.sources.pop(0)
        return None if source is None else await parse_step(source, RegisteredMacros(self.fake.channel))


def _play_with(fake: FakeGame, jev: FakeJev, repair, source: str, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(**{"no_action_grace_seconds": 0.2, **knobs})

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel))
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Press Start.", macros, repair)

    return asyncio.run(go())


def test_an_action_that_keeps_sending_nothing_calls_the_editor_and_play_goes_on_with_the_rewrite() -> None:
    fake = _menu()
    repair = Repairs(fake, MACROS)

    play = _play_with(fake, FakeJev(), repair, STUCK, repair_after=2)

    assert play.stopped == "done"
    idle, fixed, pressed, reached = play.turns
    assert idle["outcome"] == "passed" and idle["sent"] == [] and "repair" not in idle
    assert fixed["repair"] == {"action": "press_start", "attempt": 1, "replaced": True}
    assert pressed["sent"][0]["summary"] == "click(start)" and reached["done"] is True
    # editor 에는 그 action 의 turn 기록만, 연달아 걸린 만큼 간다.
    ((actions, action, turns),) = repair.calls
    assert actions == ["press_start", "look_around"] and action == "press_start"
    assert [turn["turn"] for turn in turns] == [0, 1]
    assert [log["turn"] for log in _turn_logs(fake) if "repair" in log] == [1]


def test_a_failing_action_is_repaired_before_the_failure_stop() -> None:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})  # 눌러도 아무 일이 없다
    patient = MACROS.replace(
        'wait_until(scene() == "Map", 1, "Start did not open the map.")', 'wait_until(scene() == "Menu", 1, "Not on the menu.")'
    )
    repair = Repairs(fake, patient)

    play = _play_with(fake, FakeJev(), repair, MACROS, repair_after=2, max_failures_in_a_row=2, max_turns_per_step=4)

    assert [turn["outcome"] for turn in play.turns] == ["failed", "failed", "passed", "passed"]
    assert play.turns[1]["repair"]["replaced"] is True and play.stopped == "turn limit"
    assert play.turns[1]["reason"].startswith("WAIT_TIMEOUT at press_start step 3 of 3: ")
    assert repair.calls[0][2][1]["reason"] == play.turns[1]["reason"]


def test_a_refused_rewrite_counts_and_the_failure_stop_applies() -> None:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})
    repair = Repairs(fake, None)

    play = _play_with(fake, FakeJev(), repair, MACROS, repair_after=2, max_failures_in_a_row=2)

    assert play.turns[1]["repair"] == {"action": "press_start", "attempt": 1, "replaced": False}
    assert play.stopped.startswith("2 actions failed in a row (last: press_start: WAIT_TIMEOUT")
    assert len(repair.calls) == 1


def test_repairs_are_capped_per_step_and_idle_turns_then_stop_play() -> None:
    fake = _menu()
    repair = Repairs(fake, STUCK, STUCK)

    play = _play_with(
        fake, FakeJev(), repair, STUCK, repair_after=2, max_repairs_per_step=1, max_idle_turns_in_a_row=4
    )

    assert len(repair.calls) == 1 and play.turns[1]["repair"]["attempt"] == 1
    # 고친 뒤 idle 수는 0 부터 다시 센다: turn 2–5 가 네 번이다.
    assert len(play.turns) == 6 and all(turn["sent"] == [] for turn in play.turns)
    assert play.stopped == "no progress: 4 actions in a row ran without sending the game an input (last: press_start)"
    assert fake.actions == []


def test_without_a_repair_idle_turns_stop_play() -> None:
    play = _play_with(_menu(), FakeJev(), None, STUCK, max_idle_turns_in_a_row=3)

    assert len(play.turns) == 3
    assert play.stopped.startswith("no progress: 3 actions in a row")


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

    # `analysis="off"`: with it on, the run review would ask the judge once at the end. What the
    # refused step itself does with the judge — nothing — is what this test pins.
    _run(fake, Asked(no_goal, no_goal), judge, FakeJev(), max_rewrites=1, analysis="off")

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


@pytest.mark.parametrize(("ending", "outcome"), [
    ('require(scene() == "Missing", "wrong scene")', "failed"),
    ('checkpoint("wait for cards")', "paused"),
])
def test_flags_survive_failed_and_paused_actions_in_turn_evidence(ending: str, outcome: str) -> None:
    from app.agents.editor.repair import evidence_lines

    source = (
        'def press_start() -> None:\n    """Prepare a spell."""\n'
        '    flag("no compatible cards")\n    ' + ending + '\n\n'
        'def done() -> bool:\n    return scene() == "Map"\n'
    )
    fake = _menu()
    play = _play(fake, FakeJev(), source, max_turns_per_step=1)
    turn = play.turns[0]
    assert turn["outcome"] == outcome
    assert turn["flags"][0]["message"] == "no compatible cards"
    assert "no compatible cards" in evidence_lines([turn])[0]
    assert _turn_logs(fake)[0]["flags"] == turn["flags"]
